"""Registration: tools, /watch, the aux task, and one watcher per process."""

from __future__ import annotations

import json
import logging
import threading

from . import onboarding
from . import models
from . import projectionist
from . import queue_prep
from . import store
from .config import Settings
from .watcher import Watcher

log = logging.getLogger(__name__)

_watcher_lock = threading.Lock()
_watcher: Watcher | None = None
_conn = None


def _connection():
    global _conn
    if _conn is None:
        _conn = store.connect()
    return _conn


def _status() -> str:
    conn = _connection()
    session = store.latest_session(conn)
    from . import models

    return json.dumps({
        "ok": True,
        "watcher_running": store.watcher_running(conn),
        "watcher_state": store.get_state(conn, "watcher_state", "unknown"),
        "claimed_session": store.get_state(conn, "claimed_session", ""),
        "session": session,
        **models.status_payload(),
    })


def _start() -> str:
    conn = _connection()
    store.set_state(conn, "suppressed_session", "")
    claimed = store.get_state(conn, "claimed_session", "")
    if claimed:
        return json.dumps({"ok": True, "message": "already claimed", "session_id": claimed})
    session = store.latest_session(conn)
    if not session:
        return json.dumps({"ok": False, "error": "no_active_session"})
    store.upsert_session(conn, {**session, "claimed": 1, "claimed_at": int(__import__("time").time())})
    store.set_state(conn, "claimed_session", session["session_id"])
    return json.dumps({"ok": True, "session_id": session["session_id"]})


def _stop() -> str:
    conn = _connection()
    session = store.latest_session(conn)
    if session:
        # Tell the watcher not to re-claim this same stream after the user ended it.
        store.set_state(conn, "suppressed_session", session["session_id"])
    store.set_state(conn, "claimed_session", "")
    with conn:
        conn.execute("UPDATE session SET claimed=0, state='ended' WHERE claimed=1")
    return json.dumps({"ok": True})


def _now() -> str:
    conn = _connection()
    session = store.latest_session(conn)
    if not session:
        return json.dumps({"ok": False, "error": "no_active_session"})
    messages = store.messages_for(conn, session["rating_key"])
    return json.dumps({"ok": True, "session": session, "messages": messages})


def _setup() -> str:
    """Text version of the onboarding flow: code, link, poll, verify, save.

    Blocks until the flow finishes (or the ~5 minute PIN timeout). The token
    is never part of the returned text.
    """
    lines: list[str] = []
    try:
        client_id = onboarding.client_identifier()
        pin = onboarding.create_pin(client_id)
        lines.append(f"Go to {onboarding.LINK_URL} and enter code {pin['code']} "
                     "(the code expires in a few minutes)")
        token = onboarding.poll_pin_token(pin["id"], client_id)
        lines.append("Account linked. Finding your Plex server…")
        servers = onboarding.filter_server_resources(onboarding.fetch_resources(token, client_id))
        if not servers:
            raise onboarding.PlexError("no Plex servers found on this account")
        choice = onboarding.choose_server(servers, token, client_id)
        server = choice["server"]
        lines.append(f"Using {server['name']} ({server['access']}) at {choice['uri']}"
                     + (" — relay fallback, this will be slow" if choice["relay_fallback"] else ""))
        verified = onboarding.verify_server(choice["uri"], token, client_id)
        names = ", ".join(verified["library_names"]) or "no libraries"
        lines.append(f"Libraries: {verified['library_count']} ({names}).")
        sample = verified["readiness"]
        lines.append(f"Readiness: {sample['note']}")
        onboarding.save_credentials(base_url=choice["uri"], token=token, client_id=client_id,
                                    server_name=server["name"], machine_id=server["machine_id"])
        lines.append("Credentials saved — the watcher picks them up on its next poll.")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 - the chat gets the clean message
        return onboarding.user_message(exc)


TOOLS = [
    (
        "watch_with_me_status",
        {
            "name": "watch_with_me_status",
            "description": "Report the watcher state, the claimed Plex session, and whether anything is playing.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        lambda: _status(),
    ),
    (
        "watch_with_me_start",
        {
            "name": "watch_with_me_start",
            "description": "Claim the current Plex session for watch-with-me.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        lambda: _start(),
    ),
    (
        "watch_with_me_stop",
        {
            "name": "watch_with_me_stop",
            "description": "End the claimed watch-with-me session.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        lambda: _stop(),
    ),
    (
        "watch_with_me_now",
        {
            "name": "watch_with_me_now",
            "description": "Show the claimed session and any messages built for it.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        lambda: _now(),
    ),
]


def _watch_command(raw_args: str) -> str:
    cmd = (raw_args or "status").strip().split()[0].lower()
    handlers = {"status": _status, "start": _start, "stop": _stop, "now": _now, "setup": _setup}
    handler = handlers.get(cmd)
    if handler is None:
        return "usage: /watch status|start|stop|now|setup"
    return handler()


def _completer_factory(ctx, settings: Settings):
    """The preparation worker's note completer, backed by the host LLM's
    ``watch_with_me_cues`` auxiliary task. Returns None when no model
    resolves: preparation waits rather than sending unrouted calls."""
    def factory():
        try:
            complete, choice = models.gateway_completer(ctx, settings)
        except Exception:  # noqa: BLE001 - the worker backs off and retries
            return None
        return complete if choice.model else None

    return factory


def _ensure_watcher(settings: Settings, ctx=None) -> None:
    global _watcher
    with _watcher_lock:
        if _watcher is not None and _watcher.is_alive():
            return
        prepare_worker = None
        if settings.prepare_depth != "off" and ctx is not None:
            prepare_worker = queue_prep.PreparationWorker(
                settings, complete_factory=_completer_factory(ctx, settings))
        watcher = Watcher(settings, prepare_worker=prepare_worker)
        if watcher.start():
            _watcher = watcher


def register(ctx) -> None:
    ctx.register_auxiliary_task(
        "watch_with_me_cues",
        display_name="Watch With Me cues",
        description=("Writes the notes. Runs once per transcript segment, about 40 calls per "
                     "film, and doubles if entity expansion is on. A flash-tier model is "
                     "sufficient; a reasoning model costs several times more and can return "
                     "empty content when the token budget goes to reasoning tokens."),
        inherit_from="compression",
    )
    ctx.register_auxiliary_task(
        "watch_with_me_ask",
        display_name="Watch With Me asks",
        description=("Answers one-tap \"what did I just see?\" Projectionist asks: one call "
                     "per tap (cached per moment). Inherits the cues model until pinned; when "
                     "the Plex server serves timeline frames, choose a vision-capable model "
                     "here or the ask degrades to transcript-only."),
        inherit_from="watch_with_me_cues",
    )

    for name, schema, handler in TOOLS:
        ctx.register_tool(
            name=name,
            toolset="watch-with-me",
            schema=schema,
            handler=lambda args, _h=handler, **_kw: _h(),
        )

    ctx.register_command("watch", _watch_command,
                         description="Watch With Me: status|start|stop|now|setup",
                         args_hint="status|start|stop|now|setup")

    settings = Settings.from_dict(ctx.get_config("settings", {}) or {})
    store.set_state(_connection(), "settings_json", json.dumps(settings.as_dict()))
    projectionist.install_llm(getattr(ctx, "llm", None))
    _ensure_watcher(settings, ctx)

    def stop() -> None:
        global _watcher
        if _watcher is not None:
            _watcher.stop()
            _watcher = None

    ctx.on_unload(stop)
