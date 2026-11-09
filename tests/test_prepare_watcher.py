"""Watcher-level preparation gating: the watcher drives the preparation
worker with playing/idle signals from its own poll loop."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from watch_with_me import store, watcher as watcher_mod
from watch_with_me.config import Settings


class FakePlex(BaseHTTPRequestHandler):
    playing = True

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.startswith("/status/sessions") and FakePlex.playing:
            payload = {"MediaContainer": {"Metadata": [{
                "ratingKey": "77", "type": "movie", "title": "Fake Feature",
                "duration": 6_000_000, "viewOffset": 60_000,
                "Player": {"state": "playing", "title": "Test Player",
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


class StubPrepareWorker:
    """Duck-typed stand-in for queue_prep.PreparationWorker."""

    def __init__(self):
        self.signals = []

    def set_playing(self, playing, *, session_ended=False):
        self.signals.append((playing, session_ended))

    def start(self):
        pass

    def is_alive(self):
        return False

    def stop(self):
        pass

    def join(self, timeout=None):
        pass


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _watcher(tmp_path, monkeypatch, base_url, worker):
    conn = store.connect(tmp_path / "data.db")
    creds = tmp_path / "plex-credentials.json"
    creds.write_text(json.dumps({"base_url": base_url, "token": "t", "client_id": "c"}))
    monkeypatch.setattr(watcher_mod, "credentials_path", lambda: creds)
    monkeypatch.setattr(watcher_mod.store, "connect", lambda: conn)
    monkeypatch.setattr(watcher_mod.Watcher, "_acquire_lock", lambda self: True)
    settings = Settings(candidate_seconds=0, poll_active_seconds=0.05,
                        poll_idle_seconds=0.05, allow_loopback_plex=True,
                        allowed_user_ids=["jason"])
    w = watcher_mod.Watcher(settings, prepare_worker=worker)
    return w, conn


def test_preparation_pauses_during_playback_and_resumes_after(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePlex)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakePlex.playing = True
    worker = StubPrepareWorker()
    w, conn = _watcher(tmp_path, monkeypatch, f"http://127.0.0.1:{server.server_address[1]}",
                       worker)
    assert w.start()
    try:
        assert _wait_for(lambda: any(s[0] for s in worker.signals))
        # The session ends: the worker must hear (idle, session_ended=True) so
        # it re-reads the queue — that signal is what picks up new episodes.
        FakePlex.playing = False
        assert _wait_for(lambda: any((not s[0]) and s[1] for s in worker.signals))
    finally:
        w.stop()
        w.join(timeout=3)
    server.shutdown()
    assert not w.is_alive()


def test_watcher_without_prepare_worker_keeps_working(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePlex)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakePlex.playing = False
    w, conn = _watcher(tmp_path, monkeypatch, f"http://127.0.0.1:{server.server_address[1]}",
                       None)
    assert w.start()
    try:
        assert _wait_for(lambda: store.get_state(conn, "watcher_state") == "idle")
    finally:
        w.stop()
        w.join(timeout=3)
    server.shutdown()
    assert not w.is_alive()
