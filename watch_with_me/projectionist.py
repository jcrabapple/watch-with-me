"""Ask the Projectionist: one-tap answers about the moment being watched.

The viewer taps one button; the engine answers "what did I just see?" from
three gated inputs and nothing else:

- a frame from the film at the predicted playhead (only when the Plex server
  serves real timeline frames — see ``PlexClient.timeline_probe``);
- the last 30 seconds of elapsed transcript, never beyond the playhead;
- claims unlocked at that moment (``store.claims_unlocked_at``) plus notes
  that already streamed, filtered by :func:`notes_for_ask`.

Escrow rules (operator decision, phase 7 verification):

- claims with ``reveal_state`` live/locked-past-anchor may enter;
- notes with ``reveal_state`` locked or credits NEVER enter an ask;
- entity-expansion notes (``kind='context'``, ``source_name='wikipedia:<name>'``)
  are classified at build time like every other claim (live with
  ``unlock_anchor_ms`` at the mention line); rows persisted before that
  classifier existed carry ``reveal_state='live'`` regardless, so they are
  still admitted only when their subject appears in the elapsed transcript
  window already being sent — checked, not assumed. A note with no
  ``reveal_state`` at all gets the same check.

The answer is one model call returning strict JSON: either
``{"answer": "...", "fact_ids": [...], "confidence": 0.x}`` or
``{"answer": null, "reason": "nothing_solid"}``. Citing a fact id that was
not supplied is treated as an escrow leak attempt and downgrades the whole
answer to ``nothing`` (the same precedent ``cues.parse_response`` set).

Latency: ``POST /ask`` returns an ``ask_id`` immediately; the page polls.
One in-flight ask per session — a second tap supersedes the first (its state
becomes ``superseded`` and its result is never delivered), while the cache
(``(rating_key, playhead bucket, sorted claim/note ids, has_frame)``) makes
repeated taps near the same moment free.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time

from . import scheduler, spoilers, store, subtitles
from .transcript import parse_srt

log = logging.getLogger(__name__)

WINDOW_MS = 30_000
BUCKET_MS = 15_000
WAIT_TIMEOUT_S = 120
CACHE_MAX = 200
NOTHING_SOLID = "nothing_solid"

ASK_INSTRUCTIONS = (
    "You are the Projectionist, a second-screen companion for someone "
    "watching a film. They tapped \"what did I just see?\" at the playhead "
    "moment described below. Answer strictly from the supplied material."
)

ASK_RULES = """\
RULES:
- Output one JSON object, exactly one of:
  {"answer": "...", "fact_ids": [...], "confidence": 0.x}
  {"answer": null, "reason": "nothing_solid"}
- Answer only what the frame, the transcript window and the facts below support.
  Never invent film facts. Never describe anything after the playhead.
- "fact_ids" lists ids from FACTS that the answer quotes or rests on. Use [] when
  the answer rests on the frame or transcript alone. Never invent an id.
- One or two sentences, plain and specific. Quote a fact when one fits.
- If nothing solid can be said from the supplied material, output
  {"answer": null, "reason": "nothing_solid"} rather than guessing.
"""

# -- playhead and transcript window ---------------------------------------------

def playhead_ms(session: dict, now_monotonic: float) -> int:
    """The predicted playhead for a stored session row.

    The row's state is the machine's ("live"/"paused"); prediction runs only
    while live. Rows without an anchor (written before base tracking) fall
    back to the last reported offset.
    """
    base_at = session.get("base_at_monotonic")
    base_offset = session.get("base_offset_ms")
    if base_at is None or base_offset is None:
        return int(session.get("offset_ms") or 0)
    state = "paused" if session.get("state") == "paused" else "playing"
    return scheduler.predicted_offset_ms(int(base_offset), float(base_at), now_monotonic,
                                         state)


def window_lines(cues: list[dict], at_ms: int, window_ms: int = WINDOW_MS) -> list[dict]:
    """Transcript cues in the last ``window_ms`` before ``at_ms``, never past it."""
    return [c for c in cues if at_ms - window_ms < int(c["start_ms"]) <= at_ms]


def render_window(lines: list[dict]) -> str:
    return "\n".join(f"[{int(c['start_ms']) // 1000}] {c['text']}" for c in lines)


# -- escrow gating for notes ------------------------------------------------------

def _normalise(text: str) -> str:
    return spoilers._normalise(text)  # noqa: SLF001 - same definition, one owner


def _subject_in_text(subject: str, text: str) -> bool:
    import re

    key = _normalise(subject)
    if not key or not text:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(key)}(?!\w)", _normalise(text)))


def _entity_subject(note: dict) -> str:
    """The entity name for an entity-expansion note.

    ``source_name`` is ``wikipedia:<Entity Name>`` for both freshly built
    notes and rows persisted before the claim ledger existed, so it is the
    one field that always carries the subject.
    """
    source = str(note.get("source_name") or "")
    if note.get("kind") == "context" and source.startswith("wikipedia:"):
        return source.split(":", 1)[1].strip()
    return ""


def _note_subject(note: dict) -> str:
    return (str(note.get("subject_key") or "").strip()
            or _entity_subject(note)
            or str(note.get("subject") or "").strip())


def notes_for_ask(notes: list[dict], playhead: int, window_text: str) -> list[dict]:
    """Notes that may enter an ask prompt at ``playhead``.

    Locked/credits notes never enter. Live non-entity notes enter as-is.
    Entity notes — classified at build time since phase 8, but legacy rows
    carry ``reveal_state='live'`` from the column default — enter only when
    their subject appears in the transcript window already being sent.
    """
    out: list[dict] = []
    for note in notes:
        state = note.get("reveal_state")
        if state in (spoilers.REVEAL_LOCKED, spoilers.REVEAL_CREDITS):
            continue
        if state == spoilers.REVEAL_LIVE and not _entity_subject(note):
            out.append(note)
            continue
        subject = _note_subject(note)
        if subject and _subject_in_text(subject, window_text):
            out.append(note)
    return out


# -- prompt and response -----------------------------------------------------------

def build_ask_prompt(*, title: str, playhead: int, lines: list[dict],
                     claims: list[dict], notes: list[dict], has_frame: bool) -> str:
    window = render_window(lines) or "(no dialogue in the last 30 seconds)"
    claim_lines = "\n".join(
        f"{c.get('claim_id') or c.get('id')} [{c.get('source_name') or c.get('source') or 'unknown'}] {c['text']}"
        for c in claims) or "(none unlocked at this moment)"
    note_lines = "\n".join(
        f"- [{n.get('source_name') or 'note'}] {n['text']}" for n in notes) or "(none)"
    basis = ("FRAME: the attached image is the frame at the playhead."
             if has_frame else
             "No frame is available; answer from the transcript window and the facts alone.")
    return (
        f"TITLE: {title}\n"
        f"PLAYHEAD: {int(playhead) // 1000}s\n"
        f"{ASK_RULES}\n"
        f"{basis}\n\n"
        f"TRANSCRIPT WINDOW (the last {WINDOW_MS // 1000}s before the playhead)\n{window}\n\n"
        f"FACTS (unlocked at this moment; quote with their ids)\n{claim_lines}\n\n"
        f"NOTES (already shown on screen, sourced)\n{note_lines}\n"
    )


def _extract_json(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no json object")
    return raw[start:end + 1]


def parse_ask_response(raw: str, allowed_ids: set[str]) -> dict:
    """Strict JSON in, normalised outcome; malformed input is ``nothing``."""
    nothing = lambda reason: {"status": "nothing", "reason": reason}  # noqa: E731
    try:
        payload = json.loads(_extract_json(raw or ""))
    except ValueError:
        return nothing("unparsable_json")
    if not isinstance(payload, dict):
        return nothing("not_an_object")
    answer = payload.get("answer")
    if answer is None:
        return nothing(str(payload.get("reason") or NOTHING_SOLID))
    if not isinstance(answer, str):
        return nothing("bad_answer")
    answer = " ".join(answer.split())
    if not answer:
        return nothing("empty_answer")
    fact_ids = payload.get("fact_ids") or []
    if not isinstance(fact_ids, list):
        return nothing("bad_fact_ids")
    ids = [str(fid) for fid in fact_ids]
    # Escrow: citing an id that was not supplied is a leak attempt (ids are
    # guessable); drop the whole answer rather than keep unsourced text.
    if any(fid not in allowed_ids for fid in ids):
        return nothing("unsourced_fact_cited")
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    return {"status": "answered", "answer": answer, "fact_ids": ids,
            "confidence": confidence}


# -- cache -------------------------------------------------------------------------

def cache_key(rating_key: str, playhead: int, claim_ids: list[str],
              note_ids: list[str], has_frame: bool) -> tuple:
    return (str(rating_key), int(playhead) // BUCKET_MS,
            tuple(sorted(claim_ids)), tuple(sorted(note_ids)), bool(has_frame))


# -- the engine --------------------------------------------------------------------

class AskUnavailable(RuntimeError):
    def __init__(self, reason: str, status: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


_installed_llm = None


def install_llm(llm) -> None:
    """plugin.register() stashes the host-owned PluginLlm for the dashboard."""
    global _installed_llm
    _installed_llm = llm


def installed_llm():
    return _installed_llm


class AskEngine:
    """Session-scoped ask lifecycle: supersede, cache, background workers.

    ``connect`` returns a SQLite connection, ``complete(prompt, frame, mime)``
    runs the model call, ``frames`` (optional) probes/fetches Plex frames.
    All three are injected so tests never touch the live DB or the network.
    """

    def __init__(self, *, connect, settings, complete, frames=None,
                 transcripts=None, monotonic=None) -> None:
        self._connect = connect
        self._settings = settings
        self._complete = complete
        self._frames = frames
        self._transcripts = transcripts
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._asks: dict[str, dict] = {}
        self._by_session: dict[str, str] = {}
        self._cache: dict[tuple, dict] = {}
        self._frame_support: dict[str, bool] = {}

    # -- public API -------------------------------------------------------------
    def start_ask(self) -> dict:
        conn = self._connect()
        session = store.latest_session(conn)
        if not session or not session["claimed"]:
            raise AskUnavailable("no_claimed_session", 409)
        ask_id = secrets.token_hex(8)
        with self._lock:
            self._prune_locked()
            previous = self._by_session.get(session["session_id"])
            if previous and previous in self._asks:
                self._asks[previous]["state"] = "superseded"
            self._by_session[session["session_id"]] = ask_id
            self._asks[ask_id] = {
                "ask_id": ask_id,
                "state": "asking",
                "session_id": session["session_id"],
                "rating_key": session["rating_key"],
                "started_at": time.time(),
            }
        threading.Thread(target=self._run, args=(ask_id, dict(session)),
                         name=f"wwm-ask-{ask_id[:6]}", daemon=True).start()
        return dict(self._asks[ask_id])

    def get(self, ask_id: str) -> dict | None:
        with self._lock:
            ask = self._asks.get(ask_id)
            return dict(ask) if ask else None

    # -- worker -------------------------------------------------------------------
    def _run(self, ask_id: str, session: dict) -> None:
        try:
            result = self._compute(ask_id, session)
        except Exception:  # noqa: BLE001 - an ask must never surface a traceback
            log.exception("projectionist: ask %s failed", ask_id)
            result = {"status": "nothing", "reason": "error"}
        with self._lock:
            ask = self._asks.get(ask_id)
            if ask is not None and ask["state"] != "superseded":
                ask.update(result)
                ask["state"] = result["status"]

    def _superseded(self, ask_id: str) -> bool:
        with self._lock:
            ask = self._asks.get(ask_id)
            return ask is None or ask["state"] == "superseded"

    def _compute(self, ask_id: str, session: dict) -> dict:
        conn = self._connect()
        rating_key = str(session["rating_key"])
        playhead = playhead_ms(session, self._monotonic())
        duration = int(session.get("duration_ms") or 0)
        if duration > 0:
            playhead = min(playhead, duration)

        cues = self._transcript_cues(conn, rating_key)
        lines = window_lines(cues, playhead)
        window_text = render_window(lines)
        claims = store.claims_unlocked_at(conn, rating_key, playhead)
        notes = notes_for_ask(store.timeline_for(conn, rating_key), playhead, window_text)

        frame, mime = self._frame(rating_key, playhead)
        has_frame = frame is not None
        key = cache_key(rating_key, playhead, [c["claim_id"] for c in claims],
                        [n["id"] for n in notes], has_frame)

        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                entry = {"state": "pending", "event": threading.Event(), "result": None}
                self._cache[key] = entry
                if len(self._cache) > CACHE_MAX:
                    self._cache.pop(next(iter(self._cache)))
                owner = True
            else:
                owner = False

        if not owner:
            # Another ask owns this moment; adopt its result when it lands.
            if not entry["event"].wait(WAIT_TIMEOUT_S):
                return {"status": "nothing", "reason": "wait_timeout"}
            result = entry["result"]
        else:
            result = None
            try:
                result = self._answer(session, playhead, lines, claims, notes,
                                      frame, mime, has_frame)
            except Exception:  # noqa: BLE001 - the owner must never strand waiters
                log.exception("projectionist: ask %s model call failed", ask_id)
                result = {"status": "nothing", "reason": "model_error"}
            finally:
                with self._lock:
                    entry["result"] = result
                    entry["state"] = "done"
                    entry["event"].set()
        if self._superseded(ask_id):
            # The computation stands in the cache for other taps; this ask's
            # own result is discarded — a newer tap owns the viewer.
            return {"status": "superseded"}
        return result

    def _answer(self, session, playhead, lines, claims, notes, frame, mime,
                has_frame) -> dict:
        title = session.get("media_title") or session.get("grandparent_title") \
            or session.get("title") or "this title"
        prompt = build_ask_prompt(title=title, playhead=playhead, lines=lines,
                                  claims=claims, notes=notes, has_frame=has_frame)
        raw = self._complete(prompt, frame, mime)
        parsed = parse_ask_response(raw, {c["claim_id"] for c in claims})
        if parsed["status"] == "nothing":
            parsed["basis"] = "transcript" if not has_frame else "frame+transcript"
            return parsed
        by_id = {c.get("claim_id") or c.get("id"): c for c in claims}
        sources = []
        for fid in parsed["fact_ids"]:
            claim = by_id.get(fid) or {}
            url = claim.get("source_url")
            entry = {"fact_id": fid, "source_name": claim.get("source_name") or "fact",
                     "source_url": url if isinstance(url, str) else None}
            if entry not in sources:
                sources.append(entry)
        parsed["sources"] = sources
        parsed["playhead_ms"] = playhead
        parsed["basis"] = "frame+transcript+claims" if has_frame else "transcript+claims"
        return parsed

    def _transcript_cues(self, conn, rating_key: str) -> list[dict]:
        languages = list(getattr(self._settings, "subtitle_languages", None) or ["en"])
        hit = subtitles.cached_transcript(conn, rating_key, languages)
        if hit is None and self._transcripts is not None:
            # Titles built straight from a Plex sidecar predate the transcript
            # cache: fetch read-only from Plex (the fetcher may populate the
            # cache for the next ask).
            try:
                text = self._transcripts.fetch(rating_key)
            except Exception:  # noqa: BLE001 - transcript is optional by design
                log.warning("projectionist: transcript fetch failed for %s", rating_key)
                text = None
            return parse_srt(text) if text else []
        if hit is None:
            return []
        return parse_srt(hit[0])

    def _frame(self, rating_key: str, playhead: int) -> tuple[bytes | None, str | None]:
        if self._frames is None:
            return None, None
        supported = self._frame_support.get(rating_key)
        if supported is None:
            try:
                supported = bool(self._frames.probe(rating_key))
            except Exception:  # noqa: BLE001 - probing is best-effort
                supported = False
            self._frame_support[rating_key] = supported
        if not supported:
            return None, None
        try:
            fetched = self._frames.fetch(rating_key, playhead)
        except Exception:  # noqa: BLE001 - a frame is optional by design
            log.warning("projectionist: frame fetch failed for %s", rating_key)
            return None, None
        if not fetched or not fetched[0]:
            return None, None
        return fetched[0], fetched[1] if len(fetched) > 1 else "image/jpeg"

    def _prune_locked(self) -> None:
        """Drop asks older than an hour (must be called under the lock)."""
        cutoff = time.time() - 3600
        stale = [aid for aid, ask in self._asks.items()
                 if ask.get("started_at", 0) < cutoff]
        for aid in stale:
            ask = self._asks.pop(aid, None)
            if ask and ask.get("session_id") in self._by_session \
                    and self._by_session[ask["session_id"]] == aid:
                self._by_session.pop(ask["session_id"], None)
