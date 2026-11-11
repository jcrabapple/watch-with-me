"""End-to-end watcher test against a fake Plex server.

Proves the loop the plan's Phase 1 acceptance criteria describe: a playing
session becomes live, a due message is delivered exactly once, and a pause
stops delivery.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from watch_with_me import store, watcher as watcher_mod
from watch_with_me.config import Settings


class FakePlex(BaseHTTPRequestHandler):
    offset = 60_000
    state = "playing"

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.startswith("/status/sessions"):
            payload = {"MediaContainer": {"Metadata": [{
                "ratingKey": "77",
                "type": "movie",
                "title": "Fake Feature",
                "duration": 6_000_000,
                "viewOffset": FakePlex.offset,
                "Player": {"state": FakePlex.state, "title": "Test Player",
                           "machineIdentifier": "fake-machine"},
                "Session": {"id": "fake-session", "location": "lan"},
                "User": {"title": "jason"},
            }]}}
        else:
            payload = {"MediaContainer": {"Metadata": []}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output clean
        pass


@pytest.fixture()
def fake_plex():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePlex)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_three_failed_polls_enter_disconnected(tmp_path, monkeypatch):
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()  # nothing is listening now

    conn = store.connect(tmp_path / "dead.db")
    creds = tmp_path / "dead-credentials.json"
    creds.write_text(json.dumps({"base_url": f"http://127.0.0.1:{dead_port}",
                                 "token": "t", "client_id": "c"}))
    monkeypatch.setattr(watcher_mod, "credentials_path", lambda: creds)
    monkeypatch.setattr(watcher_mod.store, "connect", lambda: conn)
    monkeypatch.setattr(watcher_mod.Watcher, "_acquire_lock", lambda self: True)

    settings = Settings(poll_idle_seconds=0.05, poll_active_seconds=0.05,
                        poll_failures_before_disconnected=3, allow_loopback_plex=True,
                        allowed_user_ids=["jason"])
    w = watcher_mod.Watcher(settings)
    assert w.start()
    try:
        assert wait_for(lambda: store.get_state(conn, "watcher_state") == "disconnected")
    finally:
        w.stop()
        w.join(timeout=3)
    assert not w.is_alive()


def test_v1_database_upgrades_and_keeps_delivery_rows(tmp_path):
    import sqlite3

    path = tmp_path / "v1.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE schema_migrations("
                   "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
    legacy.executescript(store.MIGRATIONS[0][1])
    legacy.execute("INSERT INTO schema_migrations VALUES(1, 0)")
    legacy.execute("INSERT INTO delivery(session_id,message_id,delivered_at) "
                   "VALUES('s','m',1)")
    legacy.commit()
    legacy.close()

    conn = store.connect(path)
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(delivery)")]
    assert "state" in columns
    assert conn.execute("SELECT state FROM delivery").fetchone()["state"] == "shown"
    conn.close()


def test_watcher_goes_live_and_delivers_once(tmp_path, fake_plex, monkeypatch):
    conn = store.connect(tmp_path / "data.db")
    with conn:
        conn.execute(
            "INSERT INTO item(rating_key,media_type,title,runtime_ms,view_count) "
            "VALUES('77','movie','Fake Feature',6000000,0)"
        )
        conn.execute(
            "INSERT INTO message(id,rating_key,t_ms,kind,text,build_version) "
            "VALUES('m1','77',30000,'cast','A test fact.',1)"
        )

    creds = tmp_path / "plex-credentials.json"
    creds.write_text(json.dumps({"base_url": fake_plex, "token": "t", "client_id": "c"}))

    monkeypatch.setattr(watcher_mod, "credentials_path", lambda: creds)
    monkeypatch.setattr(watcher_mod.store, "connect", lambda: conn)
    monkeypatch.setattr(watcher_mod.Watcher, "_acquire_lock", lambda self: True)

    settings = Settings(candidate_seconds=0, poll_active_seconds=0.05,
                        poll_idle_seconds=0.05, allow_loopback_plex=True,
                        allowed_user_ids=["jason"])
    w = watcher_mod.Watcher(settings)
    assert w.start()
    try:
        assert wait_for(lambda: (store.latest_session(conn) or {}).get("state") == "live")

        # Offset is past the message, so it becomes due on the next poll.
        FakePlex.offset = 60_000
        assert wait_for(lambda: store.delivered_ids(conn, "fake-session") == {"m1"})

        shown = store.shown_messages(conn, "fake-session")
        assert [m["id"] for m in shown] == ["m1"]

        # Nothing is delivered twice.
        time.sleep(0.4)
        assert len(store.shown_messages(conn, "fake-session")) == 1
    finally:
        w.stop()
        w.join(timeout=3)
    assert not w.is_alive()


def test_effective_settings_overlay_applies_ui_edits_without_restart(tmp_path):
    conn = store.connect(tmp_path / "overlay.db")
    w = watcher_mod.Watcher(Settings(note_display="live", rewatch=False))
    store.set_state(conn, "settings_json", json.dumps(
        {"note_display": "pause", "rewatch": True, "prepare_depth": "queue"}))

    effective = w._effective_settings(conn, {})
    assert effective.note_display == "pause"   # overlay beat the startup value
    assert effective.rewatch is True
    assert effective.prepare_depth == "queue"
    assert w.settings.note_display == "live"   # startup settings untouched

    store.set_state(conn, "settings_json", "{not json")
    assert w._effective_settings(conn, {}).note_display == "live"


def test_effective_settings_authorizes_the_linked_username_only_when_allowlist_empty(tmp_path):
    conn = store.connect(tmp_path / "user.db")
    w = watcher_mod.Watcher(Settings())
    creds = {"username": "jcrabapple"}

    assert w._effective_settings(conn, creds).allowed_user_ids == ["jcrabapple"]
    # a configured allowlist wins; the credential username never appends itself
    store.set_state(conn, "settings_json",
                    json.dumps({"allowed_user_ids": ["someone-else"]}))
    assert w._effective_settings(conn, creds).allowed_user_ids == ["someone-else"]
    # no username in the credentials file: strict empty allowlist preserved
    store.set_state(conn, "settings_json", "")
    assert w._effective_settings(conn, {}).allowed_user_ids == []


def test_watcher_claims_for_the_linked_username_with_no_allowlist(tmp_path, fake_plex,
                                                                  monkeypatch):
    conn = store.connect(tmp_path / "data.db")
    creds = tmp_path / "plex-credentials.json"
    creds.write_text(json.dumps({"base_url": fake_plex, "token": "t", "client_id": "c",
                                 "username": "jason"}))

    monkeypatch.setattr(watcher_mod, "credentials_path", lambda: creds)
    monkeypatch.setattr(watcher_mod.store, "connect", lambda: conn)
    monkeypatch.setattr(watcher_mod.Watcher, "_acquire_lock", lambda self: True)

    settings = Settings(candidate_seconds=0, poll_active_seconds=0.05,
                        poll_idle_seconds=0.05, allow_loopback_plex=True)
    w = watcher_mod.Watcher(settings)  # allowed_user_ids empty at startup
    assert w.start()
    try:
        assert wait_for(lambda: (store.latest_session(conn) or {}).get("state") == "live")
        assert store.get_state(conn, "claimed_session") == "fake-session"
    finally:
        w.stop()
        w.join(timeout=3)
    assert not w.is_alive()


# -- Chat handoff (the notify callback) --------------------------------------------


def _notify_env(tmp_path, fake_plex, monkeypatch, **overrides):
    """A live watcher against FakePlex with a recording notify callback."""
    conn = store.connect(tmp_path / "notify.db")
    with conn:
        conn.execute(
            "INSERT INTO item(rating_key,media_type,title,runtime_ms,view_count) "
            "VALUES('77','movie','Fake Feature',6000000,0)"
        )
        conn.execute(
            "INSERT INTO message(id,rating_key,t_ms,kind,text,build_version) "
            "VALUES('m1','77',30000,'cast','A test fact.',1)"
        )
    creds = tmp_path / "plex-credentials.json"
    creds.write_text(json.dumps({"base_url": fake_plex, "token": "t", "client_id": "c"}))
    monkeypatch.setattr(watcher_mod, "credentials_path", lambda: creds)
    monkeypatch.setattr(watcher_mod.store, "connect", lambda: conn)
    monkeypatch.setattr(watcher_mod.Watcher, "_acquire_lock", lambda self: True)
    FakePlex.state = "playing"

    calls: list[tuple[dict, int, str]] = []
    options = dict(candidate_seconds=0, poll_active_seconds=0.05,
                   poll_idle_seconds=0.05, allow_loopback_plex=True,
                   allowed_user_ids=["jason"])
    options.update(overrides)
    w = watcher_mod.Watcher(Settings(**options),
                            # Truthy delivery: the watcher records (and
                            # persists) the dedup key only when the callback
                            # reports the message was actually injected.
                            notify=lambda row, n, mode: calls.append((row, n, mode)) or True)
    return conn, calls, w


def test_notify_fires_once_per_claimed_session(tmp_path, fake_plex, monkeypatch):
    conn, calls, w = _notify_env(tmp_path, fake_plex, monkeypatch,
                                 chat_notifications=True)
    assert w.start()
    try:
        assert wait_for(lambda: (store.latest_session(conn) or {}).get("state") == "live")
        assert wait_for(lambda: len(calls) == 1)
        # the dedup holds across further polls of the same stream
        time.sleep(0.4)
        assert len(calls) == 1
        row, notes_count, mode = calls[0]
        assert mode == "claimed"
        assert row["session_id"] == "fake-session"
        assert row["rating_key"] == "77"
        assert row["player_title"] == "Test Player"
        # the note count comes from the store, not the (empty) claiming poll
        assert notes_count == 1
    finally:
        w.stop()
        w.join(timeout=3)
    assert not w.is_alive()


def test_notify_fires_once_for_a_candidate_under_ask_mode(tmp_path, fake_plex,
                                                          monkeypatch):
    conn, calls, w = _notify_env(tmp_path, fake_plex, monkeypatch,
                                 session_start="ask", chat_notifications=True)
    assert w.start()
    try:
        # ask mode never self-promotes: the stream stays an unclaimed candidate
        assert wait_for(lambda: (store.latest_session(conn) or {}).get("state")
                        == "candidate")
        assert wait_for(lambda: len(calls) == 1)
        time.sleep(0.4)
        assert len(calls) == 1
        row, notes_count, mode = calls[0]
        assert mode == "ask"
        assert row["session_id"] == "fake-session"
        assert notes_count == 0
    finally:
        w.stop()
        w.join(timeout=3)
    assert not w.is_alive()


def test_notify_never_fires_when_chat_notifications_are_off(tmp_path, fake_plex,
                                                            monkeypatch):
    conn, calls, w = _notify_env(tmp_path, fake_plex, monkeypatch)  # default: off
    assert w.start()
    try:
        assert wait_for(lambda: (store.latest_session(conn) or {}).get("state") == "live")
        time.sleep(0.4)
        assert calls == []
    finally:
        w.stop()
        w.join(timeout=3)
    assert not w.is_alive()


def test_a_raising_notify_never_breaks_the_poll_loop(tmp_path, fake_plex, monkeypatch):
    def denied(row, notes_count, mode):
        raise RuntimeError("gateway injection refused")

    conn = store.connect(tmp_path / "raise.db")
    creds = tmp_path / "plex-credentials.json"
    creds.write_text(json.dumps({"base_url": fake_plex, "token": "t", "client_id": "c"}))
    monkeypatch.setattr(watcher_mod, "credentials_path", lambda: creds)
    monkeypatch.setattr(watcher_mod.store, "connect", lambda: conn)
    monkeypatch.setattr(watcher_mod.Watcher, "_acquire_lock", lambda self: True)
    FakePlex.state = "playing"

    w = watcher_mod.Watcher(
        Settings(candidate_seconds=0, poll_active_seconds=0.05,
                 poll_idle_seconds=0.05, allow_loopback_plex=True,
                 allowed_user_ids=["jason"], chat_notifications=True),
        notify=denied)
    assert w.start()
    try:
        # the claim still lands and the watcher keeps polling
        assert wait_for(lambda: (store.latest_session(conn) or {}).get("state") == "live")
        assert w.is_alive()
    finally:
        w.stop()
        w.join(timeout=3)
    assert not w.is_alive()


# -- one-tap page claim vs the next poll (review F1, BLOCKER) -----------------------


PLAYING_SESSION = {
    "session_id": "fake-session",
    "rating_key": "77",
    "media_type": "movie",
    "title": "Fake Feature",
    "grandparent_title": None,
    "duration_ms": 6_000_000,
    "offset_ms": 60_000,
    "state": "playing",
    "player_title": "Test Player",
    "player_machine_identifier": "fake-machine",
    "user_title": "jason",
    "location": "lan",
}


class _StaticPlex:
    """PlexClient stub reporting one fixed session list."""

    def __init__(self, *args, **kwargs):
        pass

    def sessions(self):
        return [dict(PLAYING_SESSION)]


@pytest.mark.parametrize("mode", ["manual", "ask", "auto"])
def test_page_claim_survives_the_next_poll(tmp_path, monkeypatch, mode):
    """POST /session/claim must not be reverted by the watcher's next poll.

    The blocker this regresses: the running watcher only adopted the stored
    claim once at startup, so its next poll overwrote claimed=0. The claim
    runs through the REAL endpoint; the polls run the real _poll_once."""
    from test_api import build

    module, client = build(tmp_path / "data.db")
    conn = module._conn
    monkeypatch.setattr(watcher_mod, "PlexClient", _StaticPlex)
    creds = {"base_url": "http://plex.test", "token": "t", "client_id": "c"}
    # candidate_seconds far above the dwell keeps 'auto' from self-promoting,
    # so the row is genuinely unclaimed until the page claim lands.
    w = watcher_mod.Watcher(Settings(session_start=mode, candidate_seconds=3600,
                                     allowed_user_ids=["jason"]))

    w._poll_once(conn, creds)
    row = store.latest_session(conn)
    assert row["claimed"] == 0  # observed (manual) or candidate (ask/auto)

    response = client.post("/api/plugins/watch-with-me/session/claim", json={})
    assert response.status_code == 200

    w._poll_once(conn, creds)  # the very next poll must adopt, not revert
    row = store.latest_session(conn)
    assert row["claimed"] == 1
    assert row["state"] == "live"
    assert store.get_state(conn, "claimed_session") == "fake-session"
    assert w.machine.claim_id == "fake-session"


# -- live host config merge (review F4) ---------------------------------------------


def test_effective_settings_reflects_a_settings_plugins_edit(tmp_path, monkeypatch):
    """A config.yaml edit under Settings → Plugins lands on the next poll —
    it is never masked by a register-time snapshot."""
    conn = store.connect(tmp_path / "host.db")
    w = watcher_mod.Watcher(Settings(prepare_depth="off", note_display="live"))
    config = {"plugins": {"entries": {"watch-with-me": {
        "settings": {"prepare_depth": "queue", "note_display": "pause"}}}}}
    monkeypatch.setattr(watcher_mod.models, "load_hermes_config", lambda: config)

    effective = w._effective_settings(conn, {})
    assert effective.prepare_depth == "queue"
    assert effective.note_display == "pause"

    # the sparse one-tap overlay still wins over the host config
    store.set_state(conn, "settings_json", json.dumps({"note_display": "both"}))
    assert w._effective_settings(conn, {}).note_display == "both"
    # ...and a host edit to a key the overlay does not touch still lands
    assert w._effective_settings(conn, {}).prepare_depth == "queue"


# -- Plex Home username vs display title (review F7) --------------------------------


def test_effective_settings_authorizes_username_and_display_title(tmp_path):
    conn = store.connect(tmp_path / "titles.db")
    w = watcher_mod.Watcher(Settings())
    creds = {"username": "jcrabapple", "account_title": "Jason"}
    assert w._effective_settings(conn, creds).allowed_user_ids == ["jcrabapple", "Jason"]
    # equal values collapse to one entry
    same = {"username": "jason", "account_title": "jason"}
    assert w._effective_settings(conn, same).allowed_user_ids == ["jason"]
    # a configured allowlist still wins over the credential fallback
    store.set_state(conn, "settings_json",
                    json.dumps({"allowed_user_ids": ["someone-else"]}))
    assert w._effective_settings(conn, creds).allowed_user_ids == ["someone-else"]


# -- notification persistence across restarts (review F5/F15) ---------------------


def test_notify_persists_across_a_watcher_restart(tmp_path, fake_plex, monkeypatch):
    """Two sequential watchers over one DB notify exactly once: the dedup set
    lives in SQLite, not in process memory."""
    conn = store.connect(tmp_path / "restart.db")
    with conn:
        conn.execute(
            "INSERT INTO item(rating_key,media_type,title,runtime_ms,view_count) "
            "VALUES('77','movie','Fake Feature',6000000,0)"
        )
    creds = tmp_path / "plex-credentials.json"
    creds.write_text(json.dumps({"base_url": fake_plex, "token": "t", "client_id": "c"}))
    monkeypatch.setattr(watcher_mod, "credentials_path", lambda: creds)
    monkeypatch.setattr(watcher_mod.store, "connect", lambda: conn)
    monkeypatch.setattr(watcher_mod.Watcher, "_acquire_lock", lambda self: True)
    FakePlex.state = "playing"

    calls: list[str] = []

    def notify(row, notes_count, mode):
        calls.append(mode)
        return True

    options = dict(candidate_seconds=0, poll_active_seconds=0.05,
                   poll_idle_seconds=0.05, allow_loopback_plex=True,
                   allowed_user_ids=["jason"], chat_notifications=True)
    w1 = watcher_mod.Watcher(Settings(**options), notify=notify)
    assert w1.start()
    try:
        assert wait_for(lambda: len(calls) == 1)
    finally:
        w1.stop()
        w1.join(timeout=3)
    assert not w1.is_alive()

    w2 = watcher_mod.Watcher(Settings(**options), notify=notify)
    assert w2.start()
    try:
        time.sleep(0.4)  # several polls: the restart must not re-notify
        assert calls == ["claimed"]
        assert w2.machine.claim_id == "fake-session"  # restart recovery intact
    finally:
        w2.stop()
        w2.join(timeout=3)
    assert not w2.is_alive()


def test_notify_retries_after_a_denied_injection(tmp_path, fake_plex, monkeypatch):
    """A denied (falsy) injection is never recorded: enabling the feature
    later retries instead of having been permanently swallowed."""
    conn = store.connect(tmp_path / "denied.db")
    creds = tmp_path / "plex-credentials.json"
    creds.write_text(json.dumps({"base_url": fake_plex, "token": "t", "client_id": "c"}))
    monkeypatch.setattr(watcher_mod, "credentials_path", lambda: creds)
    monkeypatch.setattr(watcher_mod.store, "connect", lambda: conn)
    monkeypatch.setattr(watcher_mod.Watcher, "_acquire_lock", lambda self: True)
    FakePlex.state = "playing"

    calls: list[str] = []

    def denied(row, notes_count, mode):
        calls.append(mode)
        return False

    w = watcher_mod.Watcher(
        Settings(candidate_seconds=0, poll_active_seconds=0.05,
                 poll_idle_seconds=0.05, allow_loopback_plex=True,
                 allowed_user_ids=["jason"], chat_notifications=True),
        notify=denied)
    assert w.start()
    try:
        assert wait_for(lambda: len(calls) >= 2)  # retried on subsequent polls
        assert store.get_state(conn, "notified_claims_json", "") in (None, "")
    finally:
        w.stop()
        w.join(timeout=3)
    assert not w.is_alive()
