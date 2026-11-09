"""Plugin API: stable page, polling endpoints, controls.

Mounted by Hermes at /api/plugins/watch-with-me/. All routes inherit the
dashboard's own authentication; the plugin adds no token scheme of its own.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from watch_with_me import store  # noqa: E402
from watch_with_me import jev  # noqa: E402
from watch_with_me import models  # noqa: E402
from watch_with_me import onboarding  # noqa: E402
from watch_with_me import projectionist  # noqa: E402
from watch_with_me import queue_prep  # noqa: E402
from watch_with_me import subtitles  # noqa: E402
from watch_with_me.config import DOWNLOAD_SUBTITLES_HELP, Settings  # noqa: E402
from watch_with_me.plex import PlexClient, PlexError  # noqa: E402

router = APIRouter()

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' http: https: data:; connect-src 'self' ws: wss:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

_conn = None


log = logging.getLogger(__name__)


def _db():
    global _conn
    if _conn is None:
        _conn = store.connect()
    return _conn


def _settings() -> Settings:
    raw = store.get_state(_db(), "settings_json", "") or ""
    try:
        return Settings.from_dict(json.loads(raw))
    except ValueError:
        return Settings()


def _session_payload(session: dict | None) -> dict:
    conn = _db()
    running = store.watcher_running(conn)
    projectionist_on = _settings().projectionist
    if not session:
        return {
            "state": "idle",
            "claimed": False,
            "media": None,
            "offset_ms": 0,
            "server_time_ms": int(time.time() * 1000),
            "muted": False,
            "prepared": False,
            "projectionist": projectionist_on,
            "watcher_running": running,
            "watcher_state": store.get_state(conn, "watcher_state", "unknown"),
        }
    item = conn.execute("SELECT * FROM item WHERE rating_key=?", (session["rating_key"],)).fetchone()
    return {
        "session_id": session["session_id"],
        "state": session["state"],
        "claimed": bool(session["claimed"]),
        "media": {
            "rating_key": session["rating_key"],
            # A session can be live long before its timeline exists; show what
            # Plex reports rather than "unknown title" during that window.
            "title": (item["title"] if item else None)
                     or session.get("media_title")
                     or session.get("grandparent_title"),
            "series": session.get("grandparent_title"),
            "media_type": (item["media_type"] if item else None) or session.get("media_type"),
            "duration_ms": (item["runtime_ms"] if item else None) or session.get("duration_ms"),
        },
        "offset_ms": session["offset_ms"],
        "server_time_ms": int(time.time() * 1000),
        "muted": bool(session["muted"]),
        "prepared": bool(item and item["build_state"] == "ready"),
        "user": session.get("user_title"),
        "player": session.get("player_title"),
        "degraded_reason": session["degraded_reason"],
        "disconnect_reason": session["disconnect_reason"],
        "projectionist": projectionist_on,
        "watcher_running": running,
        "watcher_state": store.get_state(conn, "watcher_state", "unknown"),
    }


@router.get("/app", response_class=HTMLResponse)
async def app_page() -> HTMLResponse:
    html = (Path(__file__).parent / "app.html").read_text("utf-8")
    return HTMLResponse(html, headers={
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    })


@router.get("/app.js")
async def app_js() -> FileResponse:
    return FileResponse(Path(__file__).parent / "app.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-store"})


@router.get("/app.css")
async def app_css() -> FileResponse:
    return FileResponse(Path(__file__).parent / "app.css", media_type="text/css",
                        headers={"Cache-Control": "no-store"})


@router.get("/session")
async def session() -> JSONResponse:
    return JSONResponse(_session_payload(store.latest_session(_db())))


@router.get("/credits")
async def credits(rating_key: str = "") -> JSONResponse:
    """The end-of-session credits artifact: what the stream withheld and when
    (or whether) it unlocks. Scoped to the latest session's item unless an
    explicit rating_key is given."""
    conn = _db()
    key = rating_key
    if not key:
        session = store.latest_session(conn)
        key = session["rating_key"] if session else ""
    if not key:
        return JSONResponse({"claims": []})
    return JSONResponse({"claims": store.withheld_claims(conn, key)})


@router.get("/messages")
async def messages(after_id: str = "", limit: int = 40) -> JSONResponse:
    conn = _db()
    session = store.latest_session(conn)
    if not session or not session["claimed"]:
        return JSONResponse({"messages": [], "next_after_id": None, "has_more": False})
    rows = store.shown_messages(conn, session["session_id"], session["rating_key"],
                                limit=max(1, min(limit, 100)))
    if after_id:
        ids = [r["id"] for r in rows]
        rows = rows[ids.index(after_id) + 1:] if after_id in ids else rows
    return JSONResponse({
        "messages": rows,
        "next_after_id": rows[-1]["id"] if rows else after_id or None,
        "has_more": False,
    })


# -- Pause Reel -------------------------------------------------------------------
#
# Delivery semantics are untouched: the watcher keeps marking deliveries
# 'shown' exactly as before. These endpoints only read that ledger and track
# what the page has revealed, in a separate delivery_read table. The page
# polls /notes/pending/count for the quiet counter while playing, and calls
# /notes/pending when it is time to reveal (a pause, or a counter tap); that
# call returns the notes delivered since the last reveal and marks them read.

NOTE_DISPLAY_MODES = ("live", "pause", "both")


def _note_display() -> str:
    mode = _settings().note_display
    return mode if mode in NOTE_DISPLAY_MODES else "live"


@router.get("/notes/pending")
async def notes_pending() -> JSONResponse:
    """The accumulated unread notes for the claimed session.

    Returns exactly the notes delivered since the last read, newest group
    first, and marks them read. Mode-agnostic: the page gates on
    ``note_display`` from the payload.
    """
    conn = _db()
    session = store.latest_session(conn)
    if not session or not session["claimed"]:
        return JSONResponse({"notes": [], "note_display": _note_display()})
    notes = store.pending_notes(conn, session["session_id"], session["rating_key"],
                                mark_read=True)
    return JSONResponse({"notes": notes, "note_display": _note_display()})


@router.get("/notes/pending/count")
async def notes_pending_count() -> JSONResponse:
    """How many delivered notes the page has not revealed yet.

    Never marks anything read — the page polls this for the counter. Carries
    ``note_display`` so the page learns the mode from the same round trip.
    """
    conn = _db()
    session = store.latest_session(conn)
    count = 0
    if session and session["claimed"]:
        count = store.pending_count(conn, session["session_id"], session["rating_key"])
    return JSONResponse({"count": count, "note_display": _note_display()})


@router.post("/ask")
async def ask() -> JSONResponse:
    """Ask the Projectionist: one-tap "what did I just see?".

    Returns an ask_id immediately; the page polls /ask/{ask_id}. Gated by the
    ``projectionist`` setting (404 when off) and by a claimed session (409).
    """
    if not _settings().projectionist:
        raise HTTPException(status_code=404, detail="projectionist is disabled")
    session = store.latest_session(_db())
    if not session or not session["claimed"]:
        raise HTTPException(status_code=409, detail="no claimed session")
    try:
        return JSONResponse(_get_engine().start_ask())
    except projectionist.AskUnavailable as exc:
        raise HTTPException(status_code=exc.status, detail=exc.reason)


@router.get("/ask/{ask_id}")
async def ask_status(ask_id: str) -> JSONResponse:
    body = _get_engine().get(ask_id)
    if body is None:
        raise HTTPException(status_code=404, detail="unknown ask_id")
    return JSONResponse({"ask_id": body["ask_id"], "status": body["state"],
                         **{k: v for k, v in body.items()
                            if k not in ("ask_id", "state")}})


# -- Projectionist engine ---------------------------------------------------------
#
# Wired lazily so route tests can substitute a stub via the module global.
# The model call runs on the host-owned PluginLlm stashed by plugin.register()
# (same process — the dashboard router is served by the plugin host), routed
# through the watch_with_me_cues auxiliary slot; the frame comes from Plex
# read-only thumbnail endpoints, probed per title.

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = projectionist.AskEngine(
            connect=_db,
            settings=_settings(),
            complete=_ask_complete,
            frames=_PlexFrames(_settings()),
            transcripts=_PlexTranscripts(_settings()),
        )
    return _engine


def _ask_complete(prompt: str, frame: bytes | None, mime: str | None) -> str:
    llm = projectionist.installed_llm()
    if llm is None:
        raise RuntimeError("projectionist: no host LLM available")
    inputs = [{"type": "text", "text": prompt}]
    if frame:
        inputs.append({"type": "image", "data": frame,
                       "mime_type": mime or "image/jpeg"})
    try:
        result = llm.complete_structured(
            instructions=projectionist.ASK_INSTRUCTIONS,
            input=inputs,
            json_mode=True,
            task=models.AUX_ASK_TASK,
            purpose="watch-with-me ask",
        )
        return (result.text or "").strip()
    except Exception:
        if not frame:
            raise
        # The note model may not accept image input: degrade to a
        # transcript-only answer instead of failing the ask.
        result = llm.complete_structured(
            instructions=projectionist.ASK_INSTRUCTIONS,
            input=[{"type": "text", "text": prompt}],
            json_mode=True,
            task=models.AUX_ASK_TASK,
            purpose="watch-with-me ask (transcript-only)",
        )
        return (result.text or "").strip()


class _PlexFrames:
    """FrameFetcher over the read-only Plex thumbnail endpoints."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None

    def _get_client(self):
        if self._client is None:
            creds = onboarding.load_credentials()
            if not creds:
                return None
            self._client = PlexClient(
                creds["base_url"], creds["token"],
                creds.get("client_id", "watch-with-me"),
                allow_loopback=self._settings.allow_loopback_plex,
            )
        return self._client

    def probe(self, rating_key: str) -> bool:
        client = self._get_client()
        return bool(client) and client.timeline_probe(rating_key)

    def fetch(self, rating_key: str, ms: int):
        client = self._get_client()
        if client is None:
            return None
        return client.frame_at(rating_key, ms)


class _PlexTranscripts:
    """TranscriptFetcher: subtitle cache first (engine's own check), then a
    read-only sidecar fetch from Plex, cached for the next ask."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None

    def _get_client(self):
        if self._client is None:
            creds = onboarding.load_credentials()
            if not creds:
                return None
            self._client = PlexClient(
                creds["base_url"], creds["token"],
                creds.get("client_id", "watch-with-me"),
                allow_loopback=self._settings.allow_loopback_plex,
            )
        return self._client

    def fetch(self, rating_key: str) -> str | None:
        from watch_with_me import build  # noqa: PLC0415 - same package, lazy

        client = self._get_client()
        if client is None:
            return None
        try:
            metadata = client.metadata(rating_key)
            stream = build.sidecar_stream(metadata)
            if stream is None:
                return None
            text = client.sidecar(stream["key"], stream.get("codec") or "srt")
        except Exception:  # noqa: BLE001 - the transcript is optional
            return None
        if not text.strip():
            return None
        language = stream.get("languageCode") or "en"
        for preference in (self._settings.subtitle_languages or ["en"]):
            if subtitles.language_matches(preference, language):
                language = preference
                break
        subtitles.write_cache(_db(), rating_key, language, text)
        return text



@router.post("/control")
async def control(request: Request) -> JSONResponse:
    body = await request.json()
    action = body.get("action")
    conn = _db()
    session = store.latest_session(conn)
    if action not in ("mute", "unmute", "end"):
        raise HTTPException(status_code=400, detail="unknown action")
    if not session:
        raise HTTPException(status_code=409, detail="no active session")
    muted = 1 if action == "mute" else 0
    ended = action == "end"
    state = "ended" if ended else session["state"]
    claimed = 0 if ended else session["claimed"]
    with conn:
        conn.execute("UPDATE session SET muted=?, state=?, claimed=? WHERE session_id=?",
                     (muted, state, claimed, session["session_id"]))
    if ended:
        # Claimed=0 alone does not stop a running watcher, which holds the claim
        # in memory and would rewrite live/claimed on its next tick; suppression
        # tells the machine to end the claim and refuse to re-claim this stream.
        store.set_state(conn, "claimed_session", "")
        store.set_state(conn, "suppressed_session", session["session_id"])
    return JSONResponse({"ok": True})


@router.post("/feedback")
async def feedback(request: Request) -> JSONResponse:
    body = await request.json()
    action = body.get("action")
    message_id = body.get("message_id")
    if action not in ("useful", "noise", "spoiler") or not message_id:
        raise HTTPException(status_code=400, detail="action and message_id required")
    conn = _db()
    with conn:
        conn.execute("INSERT INTO feedback(message_id, action, at) VALUES(?,?,?)",
                     (message_id, action, int(time.time())))
    return JSONResponse({"ok": True})


@router.get("/settings")
async def settings() -> JSONResponse:
    """Model-selection state for the settings area: the resolved model (or
    "not configured" when empty), where it came from, and one guidance line.

    The ``jev`` block is masked entity-selection state: whether the optional
    decision-model feature is on, which provider it resolves to, and at most
    the key's last four characters. The key itself never appears here (or in
    any payload, log, or config file)."""
    payload = models.status_payload()
    current = _settings()
    status = jev.key_status()
    payload["jev"] = {
        "enabled": bool(current.jev_enabled),
        "provider": current.jev_provider or "auto",
        "configured": status["configured"],
        "last4": status["last4"],
    }
    return JSONResponse(payload)


@router.post("/settings/jev")
async def settings_jev(request: Request) -> JSONResponse:
    """Write-only key field for optional Jev entity selection.

    POST {"api_key": "..."} stores the key mode 0600 in the plugin data dir
    (never in config.yaml) and returns only {"configured", "last4"}. POST
    {"api_key": ""} deletes the stored key. Unreachable/missing key is never
    an error anywhere else in the plugin — this endpoint only manages the
    credential, it never gates anything.
    """
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        body = {}
    key = str(body.get("api_key") or "").strip()
    if key:
        jev.save_api_key(key)
    else:
        target = jev.api_key_path()
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
    status = jev.key_status()
    return JSONResponse({"configured": status["configured"],
                         "last4": status["last4"]})


@router.get("/queue")
async def queue() -> JSONResponse:
    """The preparation view: one line per Continue Watching item.

    States are prepared | queued | unsupported | failed, derived entirely
    from the store (prepare_seen / prepare_queue / prepare_run / item) so the
    page and the worker never disagree. ``unsupported`` carries the §12
    taxonomy remedy as its detail.
    """
    conn = _db()
    settings = _settings()
    day = queue_prep.day_key()
    return JSONResponse({
        "depth": settings.prepare_depth,
        "watcher_running": store.watcher_running(conn),
        "budget": {"day": day,
                   "used": queue_prep.runs_started_on(conn, day),
                   "limit": int(settings.prepare_budget_titles_per_day)},
        "items": queue_prep.dashboard_items(conn, settings),
    })


# -- Plex onboarding -------------------------------------------------------------
#
# A link flow runs on a daemon thread: create PIN -> show the code -> poll
# plex.tv -> discover servers -> probe connections -> verify libraries ->
# save credentials. GET /setup never blocks; it only reports the phase. The
# token exists only inside the worker and the credentials file — it is never
# placed in _setup_flow, so it can never appear in this payload.

class _LinkCancelled(Exception):
    pass


_setup_lock = threading.Lock()
_setup_flow: dict = {"phase": "idle"}


def _flow_active() -> bool:
    return _setup_flow.get("phase") in ("awaiting_link", "working")


def _cancellable_sleep(event: threading.Event):
    def sleep(interval: float) -> None:
        if event.wait(interval):
            raise _LinkCancelled()
    return sleep


def _link_worker(cancel: threading.Event) -> None:
    try:
        client_id = onboarding.client_identifier()
        pin = onboarding.create_pin(client_id)
        _setup_flow["code"] = pin["code"]
        token = onboarding.poll_pin_token(pin["id"], client_id, sleep=_cancellable_sleep(cancel))
        _setup_flow["phase"] = "working"
        servers = onboarding.filter_server_resources(onboarding.fetch_resources(token, client_id))
        if not servers:
            raise PlexError("no Plex servers found on this account")
        choice = onboarding.choose_server(servers, token, client_id)
        server = choice["server"]
        _setup_flow["server"] = {"name": server["name"], "access": server["access"]}
        verified = onboarding.verify_server(choice["uri"], token, client_id)
        onboarding.save_credentials(
            base_url=choice["uri"], token=token, client_id=client_id,
            server_name=server["name"], machine_id=server["machine_id"])
        _setup_flow.update(phase="done", error=None, code=None, server={
            "name": server["name"],
            "machine_id": server["machine_id"],
            "access": server["access"],
            "connection": choice["uri"],
            "relay_fallback": choice["relay_fallback"],
            "library_count": verified["library_count"],
            "library_names": verified["library_names"],
            "readiness": {"sampled": verified["readiness"]["sampled"],
                          "with_sidecar": verified["readiness"]["with_sidecar"],
                          "note": verified["readiness"]["note"],
                          "items": verified["readiness"]["items"]},
        })
        _setup_flow["download"] = _download_state()
        log_safe = {"phase": "done", "server": server["name"],
                    "connection": choice["uri"], "relay": choice["relay"]}
        log.info("watch-with-me link flow finished: %s", log_safe)
    except _LinkCancelled:
        _setup_flow.update(phase="idle", code=None, error=None, server=None)
    except Exception as exc:  # noqa: BLE001 - the page needs a clean message, not a traceback
        log.warning("watch-with-me link flow failed: %s", onboarding.user_message(exc))
        _setup_flow.update(phase="error", code=None,
                           error=onboarding.user_message(exc))


def _linked_server(creds: dict) -> dict:
    """What the page shows once credentials exist (plus a live library count)."""
    server = {
        "name": creds.get("server_name") or "Plex server",
        "machine_id": creds.get("machine_id") or "",
        "access": "your server",
        "connection": creds.get("base_url") or "",
        "relay_fallback": False,
        "library_count": None,
        "library_names": [],
        "readiness": None,
    }
    error = None
    try:
        client = PlexClient(creds["base_url"], creds["token"],
                            creds.get("client_id", "watch-with-me"), timeout=4.0)
        sections = client.library_sections()
        server["library_count"] = len(sections)
        server["library_names"] = [s.get("title") or "unknown" for s in sections]
        # Per-item state (sidecar/embedded/image_only/none) powers both the
        # coverage sentence and the per-item download action on the page.
        server["readiness"] = onboarding.readiness_sample(client)
    except Exception as exc:  # noqa: BLE001 - the page needs the reason, not a 500
        error = onboarding.user_message(exc)
    server["download"] = _download_state()
    return {"server": server, "error": error}


def _download_state() -> dict:
    """The subtitle-download consent block shown next to the per-item action."""
    settings = subtitles.load_settings(_db())
    return {
        "enabled": settings.download_subtitles,
        "languages": settings.subtitle_languages,
        "budget": settings.subtitle_download_budget,
        "help": DOWNLOAD_SUBTITLES_HELP,
    }


def _setup_payload() -> dict:
    creds = onboarding.load_credentials()
    payload = {"linked": bool(creds), "link": onboarding.LINK_URL,
               "code": None, "server": None, "error": None}
    if _flow_active():
        payload.update(phase=_setup_flow["phase"], code=_setup_flow.get("code"),
                       server=_setup_flow.get("server"))
        return payload
    payload["phase"] = _setup_flow.get("phase", "idle")
    if _setup_flow.get("phase") == "error":
        payload["error"] = _setup_flow.get("error")
    elif creds:
        # Right after a successful link the flow carries the freshly verified
        # server (with its readiness note); later loads re-probe from the file.
        flow_server = _setup_flow.get("server") if _setup_flow.get("phase") == "done" else None
        if flow_server and flow_server.get("readiness") is not None:
            flow_server.setdefault("download", _setup_flow.get("download"))
            payload["server"] = flow_server
            payload["error"] = None
        else:
            linked = _linked_server(creds)
            payload["server"] = linked["server"]
            payload["error"] = linked["error"]
    return payload


@router.get("/setup")
async def setup() -> JSONResponse:
    return JSONResponse(_setup_payload())


@router.post("/setup/pin")
async def setup_pin() -> JSONResponse:
    with _setup_lock:
        if _flow_active():
            return JSONResponse(_setup_payload(), status_code=200)
        _setup_flow.clear()
        _setup_flow.update({"phase": "awaiting_link", "code": None, "server": None,
                            "error": None, "cancel": threading.Event()})
        cancel = _setup_flow["cancel"]
    threading.Thread(target=_link_worker, args=(cancel,),
                     name="watch-with-me-link", daemon=True).start()
    return JSONResponse(_setup_payload())


@router.post("/setup/relink")
async def setup_relink() -> JSONResponse:
    """Replace the stored token: same flow as the first link."""
    return await setup_pin()


@router.post("/setup/cancel")
async def setup_cancel() -> JSONResponse:
    flow = _setup_flow
    if _flow_active() and flow.get("cancel") is not None:
        flow["cancel"].set()
    return JSONResponse({"ok": True})


@router.post("/setup/subtitles/download")
async def setup_subtitles_download(request: Request) -> JSONResponse:
    """The per-item "download subtitle" action.

    This is the explicit per-item consent: it works regardless of the
    ``download_subtitles`` setting (that setting only gates the *automatic*
    prepare run). It performs exactly one item's search-and-attach and
    returns the taxonomy state plus the user-facing message.
    """
    body = await request.json()
    rating_key = str(body.get("rating_key") or "")
    if not rating_key:
        raise HTTPException(status_code=400, detail="rating_key required")
    creds = onboarding.load_credentials()
    if not creds:
        raise HTTPException(status_code=409, detail="Plex is not linked yet")
    conn = _db()
    settings = subtitles.load_settings(conn)
    languages = body.get("languages") or settings.subtitle_languages or ["en"]
    client = PlexClient(creds["base_url"], creds["token"],
                        creds.get("client_id", "watch-with-me"), timeout=15.0)
    result = subtitles.download_subtitle(client, conn, rating_key, consent=True,
                                         languages=list(languages),
                                         poll_interval=1.0)
    return JSONResponse({"ok": result["state"] in ("downloaded", "cached", "sidecar"),
                         **result})
