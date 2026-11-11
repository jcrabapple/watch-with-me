"""The plugin surface: tool registration, caller-session capture, and the
chat-handoff callback's fail-open contract.

Runs without a Hermes gateway: a fake ctx stands in for the host facade and
fake ``gateway.session_context`` modules provide the bound session key.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from watch_with_me import plugin, store
from watch_with_me.config import Settings

ROW = {
    "session_id": "fake-session",
    "rating_key": "77",
    "media_title": "Fake Feature",
    "player_title": "Test Player",
}


class FakeCtx:
    """The host facade slice plugin.register() touches."""

    def __init__(self):
        self.tools = {}
        self.commands = {}
        self.aux_tasks = []
        self.injected = []
        self.unload = None
        self.inject_result = True

    def register_tool(self, *, name, toolset, schema, handler):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_auxiliary_task(self, *args, **kwargs):
        self.aux_tasks.append((args, kwargs))

    def get_config(self, *args, **kwargs):
        return {}

    def on_unload(self, fn):
        self.unload = fn

    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        self.injected.append({"content": content, "role": role,
                              "session_key": session_key})
        if isinstance(self.inject_result, Exception):
            raise self.inject_result
        return self.inject_result


class RecorderWatcher:
    """Captures the Watcher constructor call instead of starting a thread."""

    instances = []

    def __init__(self, settings, *, prepare_worker=None, notify=None):
        self.settings = settings
        self.prepare_worker = prepare_worker
        self.notify = notify
        RecorderWatcher.instances.append(self)

    def start(self):
        return True

    def is_alive(self):
        return True

    def stop(self):
        pass


@pytest.fixture()
def surface(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(plugin, "_conn", None)
    monkeypatch.setattr(plugin, "_watcher", None)
    RecorderWatcher.instances = []
    monkeypatch.setattr(plugin, "Watcher", RecorderWatcher)
    ctx = FakeCtx()
    plugin.register(ctx)
    return ctx


def _fake_gateway(monkeypatch, key):
    gateway_mod = types.ModuleType("gateway")
    session_context = types.ModuleType("gateway.session_context")
    session_context.get_session_env = lambda name, default="": \
        key if name == "HERMES_SESSION_KEY" else default
    gateway_mod.session_context = session_context
    monkeypatch.setitem(sys.modules, "gateway", gateway_mod)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)


def test_register_exposes_the_toolset_command_and_aux_tasks(surface):
    assert set(surface.tools) == {"watch_with_me_status", "watch_with_me_start",
                                  "watch_with_me_stop", "watch_with_me_now"}
    assert all(t["toolset"] == "watch-with-me" for t in surface.tools.values())
    assert set(surface.commands) == {"watch"}
    assert [a[0][0] for a in surface.aux_tasks] == ["watch_with_me_cues",
                                                    "watch_with_me_ask"]


def test_register_wires_a_notify_callback_into_the_watcher(surface):
    assert len(RecorderWatcher.instances) == 1
    assert RecorderWatcher.instances[0].notify is not None


def test_tool_invocation_records_the_calling_session_key(surface, monkeypatch):
    _fake_gateway(monkeypatch, "chat:42")
    result = surface.tools["watch_with_me_status"]["handler"]({})
    assert json.loads(result)["ok"] is True
    assert store.get_state(plugin._connection(), "notify_session_key") == "chat:42"

    # a later call from another session overwrites the handoff target
    _fake_gateway(monkeypatch, "chat:99")
    surface.tools["watch_with_me_now"]["handler"]({})
    assert store.get_state(plugin._connection(), "notify_session_key") == "chat:99"


def test_watch_command_records_the_calling_session_key(surface, monkeypatch):
    _fake_gateway(monkeypatch, "chat:42")
    result = surface.commands["watch"]("status")
    assert json.loads(result)["ok"] is True
    assert store.get_state(plugin._connection(), "notify_session_key") == "chat:42"


def test_headless_tool_call_leaves_the_stored_key_untouched(surface):
    # no gateway modules: the import fails and the recorder stays silent
    assert "gateway" not in sys.modules
    surface.tools["watch_with_me_status"]["handler"]({})
    assert store.get_state(plugin._connection(), "notify_session_key") in (None, "")


def test_headless_env_fallback_never_records_the_process_launch_key(surface,
                                                                    monkeypatch):
    """get_session_env falls back to os.environ for unbound threads; without
    a bound context that value is the process-launch env var, not a chat
    session, and must never become the notification target."""
    monkeypatch.setenv("HERMES_SESSION_KEY", "stale:launcher")
    _fake_gateway(monkeypatch, "stale:launcher")  # the unbound fallback value
    surface.tools["watch_with_me_status"]["handler"]({})
    assert store.get_state(plugin._connection(), "notify_session_key") in (None, "")


def test_bound_session_key_is_recorded_despite_a_stale_launch_env(surface,
                                                                  monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_KEY", "stale:launcher")
    _fake_gateway(monkeypatch, "chat:7")  # a genuinely bound session
    surface.tools["watch_with_me_status"]["handler"]({})
    assert store.get_state(plugin._connection(), "notify_session_key") == "chat:7"


def test_notify_injects_into_the_saved_session_with_the_companion_link(surface,
                                                                       monkeypatch):
    from watch_with_me import companion as companion_mod
    _fake_gateway(monkeypatch, "chat:42")
    surface.tools["watch_with_me_status"]["handler"]({})
    monkeypatch.setattr(companion_mod, "lan_ipv4", lambda: "192.168.1.67")

    notify = RecorderWatcher.instances[0].notify
    assert notify(ROW, 3, "claimed") is True
    assert len(surface.injected) == 1
    msg = surface.injected[0]
    assert msg["role"] == "user"
    assert msg["session_key"] == "chat:42"
    assert "Now watching Fake Feature on Test Player" in msg["content"]
    assert "3 notes ready" in msg["content"]
    assert "http://192.168.1.67:9119/api/plugins/watch-with-me/app" in msg["content"]


def test_notify_uses_the_ask_message_for_ask_mode(surface, monkeypatch):
    _fake_gateway(monkeypatch, "chat:42")
    surface.tools["watch_with_me_status"]["handler"]({})
    monkeypatch.setattr("watch_with_me.plugin.companion_url",
                        lambda req, settings: "http://example.test/app")
    notify = RecorderWatcher.instances[0].notify
    assert notify(ROW, 0, "ask") is True
    content = surface.injected[0]["content"]
    assert "Fake Feature started on Test Player" in content
    assert "/watch start" in content and "Watch this stream" in content
    assert "http://example.test/app" in content


def test_notify_falls_back_to_keyless_injection_without_a_saved_key(surface):
    notify = RecorderWatcher.instances[0].notify
    assert notify(ROW, 1, "claimed") is True
    assert surface.injected[0]["session_key"] is None


def test_notify_fails_open_when_injection_is_denied(surface, monkeypatch):
    _fake_gateway(monkeypatch, "chat:42")
    surface.tools["watch_with_me_status"]["handler"]({})
    surface.inject_result = False
    notify = RecorderWatcher.instances[0].notify
    assert notify(ROW, 1, "claimed") is False
    assert len(surface.injected) == 1  # attempted, just refused


def test_notify_fails_open_when_injection_raises(surface, monkeypatch):
    _fake_gateway(monkeypatch, "chat:42")
    surface.tools["watch_with_me_status"]["handler"]({})
    surface.inject_result = RuntimeError("gateway injection refused")
    notify = RecorderWatcher.instances[0].notify
    assert notify(ROW, 1, "claimed") is False


def test_notify_prefers_the_public_base_url_overlay(surface, monkeypatch):
    _fake_gateway(monkeypatch, "chat:42")
    surface.tools["watch_with_me_status"]["handler"]({})
    store.set_state(plugin._connection(), "settings_json",
                    json.dumps({"public_base_url": "https://wwm.example.test/"}))
    notify = RecorderWatcher.instances[0].notify
    assert notify(ROW, 1, "claimed") is True
    assert "https://wwm.example.test/api/plugins/watch-with-me/app" in \
        surface.injected[0]["content"]


def test_notify_without_an_injector_is_a_quiet_noop(surface):
    surface.inject_message = None  # host without the capability
    notify = RecorderWatcher.instances[0].notify
    assert notify(ROW, 1, "claimed") is False


# -- register() and the runtime settings overlay (review F4/F8) ---------------------


def test_register_never_snapshots_settings_into_runtime_state(surface):
    """register() must not write a full settings snapshot: the watcher and the
    API merge the LIVE host config per poll, and a snapshot would mask every
    Settings → Plugins edit until reload."""
    assert store.get_state(plugin._connection(), "settings_json", "") in (None, "")


def test_register_prunes_a_legacy_full_settings_snapshot(surface):
    """An overlay written by an older build (a full as_dict() snapshot) is
    pruned to the sparse UI keys at register time; real UI edits inside it
    survive the migration."""
    conn = plugin._connection()
    snapshot = Settings().as_dict()
    snapshot["note_display"] = "pause"     # a real drawer edit
    snapshot["prepare_depth"] = "queue"    # a real drawer edit
    snapshot["chat_notifications"] = True  # pure snapshot data: pruned
    store.set_state(conn, "settings_json", json.dumps(snapshot))

    plugin.register(surface)  # re-register on the same ctx

    overlay = json.loads(store.get_state(conn, "settings_json"))
    assert overlay.get("note_display") == "pause"
    assert overlay.get("prepare_depth") == "queue"
    assert "chat_notifications" not in overlay


def test_settings_mode_overlay_survives_register(surface, monkeypatch, tmp_path):
    """A drawer edit (POST /settings/mode) must survive a plugin reload —
    the pre-fix snapshot write reverted it on every register()."""
    from test_api import build

    module, client = build(tmp_path / "data.db")
    monkeypatch.setattr(plugin, "_conn", module._conn)

    response = client.post("/api/plugins/watch-with-me/settings/mode",
                           json={"note_display": "pause"})
    assert response.status_code == 200

    plugin.register(surface)  # simulate the gateway reloading the plugin

    overlay = json.loads(store.get_state(module._conn, "settings_json"))
    assert overlay["note_display"] == "pause"
    assert client.get("/api/plugins/watch-with-me/notes/pending/count").json()[
        "note_display"] == "pause"
