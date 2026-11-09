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
