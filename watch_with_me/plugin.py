"""Registration: tools, /watch, the aux task, and one watcher per process."""

from __future__ import annotations

import json
import logging
import os
import threading

from . import onboarding
from . import models
from . import projectionist
from . import queue_prep
from . import store
from .companion import companion_url
from .config import Settings
from .watcher import UI_OVERLAY_KEYS, Watcher

log = logging.getLogger(__name__)

_watcher_lock = threading.Lock()
_watcher: Watcher | None = None
_conn = None


def _connection():
    global _conn
    if _conn is None:
        _conn = store.connect()
    return _conn


def _record_caller_session() -> None:
    """Remember which chat session last drove the plugin.

    The watcher later hands playback-start notifications back to that session
    via ctx.inject_message. Runs inside tool/command handlers, where the
    gateway binds the calling session key in a contextvar. Never raises: a
    headless caller (cron, tests) simply leaves the stored key untouched.

    ``get_session_env`` is the documented public seam for reading session
    identity from the task-local context (plugins guide, platform plugins);
    it consults the bound ContextVar first and only falls back to the
    process env when no session was ever bound in this context. That fallback
    is exactly the hazard: an unbound thread (cron, a worker) would record
    the process-launch env var as if it were a chat session. A key that
    equals the process env value is therefore ignored — the bound value is
    essentially never identical to the launcher's own variable.
    """
    try:
        from gateway.session_context import get_session_env
        key = get_session_env("HERMES_SESSION_KEY", "")
        process_value = os.environ.get("HERMES_SESSION_KEY", "")
        if key and key != process_value:
            store.set_state(_connection(), "notify_session_key", str(key))
    except Exception:  # noqa: BLE001 - headless callers have no session context
        pass


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
        identity = onboarding.fetch_account_identity(token, client_id)
        username = identity["username"] or identity["title"]
        onboarding.save_credentials(base_url=choice["uri"], token=token, client_id=client_id,
                                    server_name=server["name"], machine_id=server["machine_id"],
                                    username=username, account_title=identity["title"])
        if username:
            lines.append(f"Linked as {username}.")
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


def _tool_call(handler):
    """Every watch_with_me_* tool entry point: remember the calling chat
    session first so playback-start notifications can find their way back."""
    _record_caller_session()
    return handler()


def _watch_command(raw_args: str) -> str:
    _record_caller_session()
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


def _chat_notify(ctx, startup: Settings):
    """Build the watcher's chat-handoff callback around ``ctx.inject_message``.

    Fails open by construction: a missing injector, a denied/False injection,
    or a raising gateway only means "no message this time" — the watcher poll
    loop and the playback session are never affected.
    """
    def notify(session_row: dict, notes_count: int, mode: str) -> bool:
        try:
            saved_key = store.get_state(_connection(), "notify_session_key", "") or ""
            title = (session_row.get("media_title")
                     or session_row.get("grandparent_title") or "your stream")
            player = session_row.get("player_title") or "Plex"
            url = _chat_companion_url(startup)
            if mode == "ask":
                text = (f"🎬 {title} started on {player}. Reply '/watch start' or tap "
                        f"'Watch this stream' on {url} to start live notes.")
            else:
                text = (f"🎬 Now watching {title} on {player} ({notes_count} notes "
                        f"ready). Open Watch With Me: {url}")
            inject = getattr(ctx, "inject_message", None)
            if inject is None:
                return False
            if saved_key:
                return bool(inject(text, role="user", session_key=saved_key))
            return bool(inject(text, role="user"))
        except Exception:  # noqa: BLE001 - notification is best-effort by design
            log.warning("watch-with-me: chat notification failed", exc_info=True)
            return False

    return notify


def _chat_companion_url(startup: Settings) -> str:
    """The link chat messages carry: the sparse runtime overlay over the live
    Settings → Plugins value over the startup setting, else LAN detection
    with the plugin default port. Either edit applies without a restart."""
    base = startup.public_base_url
    host_settings = models.live_plugin_settings()
    if host_settings.get("public_base_url"):
        base = str(host_settings["public_base_url"])
    try:
        raw = store.get_state(_connection(), "settings_json", "") or ""
        overlay = json.loads(raw)
        if isinstance(overlay, dict) and overlay.get("public_base_url"):
            base = str(overlay["public_base_url"])
    except ValueError:
        pass
    return companion_url(None, Settings.from_dict({"public_base_url": base}))


def _ensure_watcher(settings: Settings, ctx=None) -> None:
    global _watcher
    with _watcher_lock:
        if _watcher is not None and _watcher.is_alive():
            return
        prepare_worker = None
        if settings.prepare_depth != "off" and ctx is not None:
            prepare_worker = queue_prep.PreparationWorker(
                settings, complete_factory=_completer_factory(ctx, settings))
        watcher = Watcher(settings, prepare_worker=prepare_worker,
                          notify=_chat_notify(ctx, settings) if ctx is not None else None)
        if watcher.start():
            _watcher = watcher


def _prune_legacy_settings_overlay(conn) -> None:
    """Drop a full-settings snapshot from ``settings_json`` if one is present.

    Older builds wrote ``settings.as_dict()`` here at register time; that full
    snapshot masked every later Settings → Plugins edit until reload. The
    overlay is sparse by contract (only UI-written keys, see
    ``watcher.UI_OVERLAY_KEYS``), so any other key found is legacy snapshot
    data and is pruned — real UI overrides survive the migration.
    """
    raw = store.get_state(conn, "settings_json", "") or ""
    try:
        data = json.loads(raw) or {}
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    if any(key not in UI_OVERLAY_KEYS for key in data):
        store.set_state(conn, "settings_json",
                        json.dumps({k: v for k, v in data.items()
                                    if k in UI_OVERLAY_KEYS}))


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
            handler=lambda args, _h=handler, **_kw: _tool_call(_h),
        )

    ctx.register_command("watch", _watch_command,
                         description="Watch With Me: status|start|stop|now|setup",
                         args_hint="status|start|stop|now|setup")

    settings = Settings.from_dict(models.live_plugin_settings())
    # No full snapshot into runtime_state: the watcher and the dashboard API
    # read the LIVE host config (Settings → Plugins) on every poll and merge
    # the sparse overlay on top, so a settings edit lands without a reload
    # and is never masked by a stale snapshot.
    _prune_legacy_settings_overlay(_connection())
    projectionist.install_llm(getattr(ctx, "llm", None))
    _ensure_watcher(settings, ctx)

    def stop() -> None:
        global _watcher
        if _watcher is not None:
            _watcher.stop()
            _watcher = None

    ctx.on_unload(stop)
