"""Background watcher: poll Plex, drive the session machine, persist the result.

One thread per process, guarded by a file lock so a gateway process and a
plugin-host child can never both poll. The router reads the database and the
heartbeat, so it does not need to be in the same process as the watcher.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import threading
import time
from pathlib import Path

from . import onboarding
from . import queue_prep
from . import store
from .config import Settings
from .machine import CLAIMED_SESSION_KEY, SessionMachine
from .plex import PlexClient, PlexError

log = logging.getLogger(__name__)

HEARTBEAT = "watcher_heartbeat"
CREDENTIALS = onboarding.CREDENTIALS


def credentials_path() -> Path:
    # The one writer lives in onboarding; this indirection keeps the tests'
    # monkeypatch of watcher_mod.credentials_path effective.
    return onboarding.credentials_path()


def load_credentials() -> dict | None:
    return onboarding.load_credentials(path=credentials_path())


class Watcher(threading.Thread):
    def __init__(self, settings: Settings | None = None, *,
                 prepare_worker: queue_prep.PreparationWorker | None = None) -> None:
        super().__init__(name="watch-with-me-watcher", daemon=True)
        self.settings = settings or Settings()
        self.machine = SessionMachine(self.settings)
        # The preparation worker is owned here but driven by signals: the
        # watcher knows when something plays; the worker decides when to
        # build. plugin.py constructs it (it needs the host LLM factory).
        self.prepare = prepare_worker
        self._had_sessions = False
        self.stop_event = threading.Event()
        self._lock_handle = None
        self._failures = 0
        self._adopted = False
        self._explained: set[tuple[str, str]] = set()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:  # type: ignore[override]
        if not self._acquire_lock():
            log.info("watch-with-me: another watcher owns the lock; not starting")
            return False
        super().start()
        return True

    def stop(self) -> None:
        self.stop_event.set()

    def _acquire_lock(self) -> bool:
        path = store.data_dir() / "watcher.lock"
        handle = open(path, "a+")  # noqa: SIM115 - held for process lifetime
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        self._lock_handle = handle
        return True

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:  # noqa: C901 - one loop, reads top to bottom
        conn = store.connect()
        if self.prepare is not None and not self.prepare.is_alive():
            self.prepare.start()
        while not self.stop_event.is_set():
            interval = self.settings.poll_idle_seconds
            try:
                creds = load_credentials()
                if not creds:
                    store.set_state(conn, "watcher_state", "unconfigured")
                    interval = self.settings.poll_idle_seconds
                else:
                    interval = self._poll_once(conn, creds)
            except PlexError as exc:
                self._failures += 1
                log.warning("watch-with-me: poll failed (%s/%s): %s", self._failures,
                            self.settings.poll_failures_before_disconnected, exc)
                if self._failures >= self.settings.poll_failures_before_disconnected:
                    self._mark_disconnected(conn, str(exc))
                    interval = min(60.0, self.settings.poll_idle_seconds
                                   * (2 ** min(self._failures, 5)))
            except Exception:  # noqa: BLE001 - a watcher must not die silently
                log.exception("watch-with-me: unexpected watcher error")
                interval = self.settings.poll_idle_seconds
            finally:
                store.set_state(conn, HEARTBEAT, str(int(time.time())))
            self.stop_event.wait(interval)
        if self.prepare is not None:
            self.prepare.stop()
            self.prepare.join(timeout=5)

    def _poll_once(self, conn, creds: dict) -> float:
        client = PlexClient(
            creds["base_url"], creds["token"], creds.get("client_id", "watch-with-me"),
            allow_loopback=self.settings.allow_loopback_plex,
        )
        sessions = client.sessions()
        self._failures = 0

        if not self._adopted:
            self.machine.adopt(store.get_state(conn, CLAIMED_SESSION_KEY, "") or None)
            self._adopted = True

        rating_keys = [s["rating_key"] for s in sessions]
        timeline: list[dict] = []
        delivered: set[str] = set()
        if self.machine.claim_id:
            claimed = next((s for s in sessions if s["session_id"] == self.machine.claim_id), None)
            if claimed is not None:
                timeline = store.timeline_for(conn, claimed["rating_key"])
                delivered = store.delivered_ids(conn, self.machine.claim_id,
                                                claimed["rating_key"])

        plan = self.machine.step(
            sessions,
            now_mono=time.monotonic(),
            now_wall=time.time(),
            messages=timeline,
            delivered=delivered,
            view_counts=store.view_counts(conn, rating_keys),
            policy_memory=store.dismissals(conn),
            suppressed_session=store.get_state(conn, "suppressed_session", "") or None,
        )

        for row in plan.rows:
            store.upsert_session(conn, row)
        for message in plan.emit:
            store.mark_delivery(conn, self.machine.claim_id or "", message["id"], "shown")
        for message in plan.skip:
            store.mark_delivery(conn, self.machine.claim_id or "", message["id"], "skipped")
        for session_id in plan.ended:
            store.mark_session_ended(conn, session_id)
        for session_id in plan.reset:
            store.clear_deliveries(conn, session_id)
        if plan.clear_suppression:
            store.set_state(conn, "suppressed_session", "")

        store.set_state(conn, CLAIMED_SESSION_KEY, self.machine.claim_id or "")
        store.set_state(conn, "watcher_state", plan.watcher_state)
        self._explain_unclaimed(plan)
        self._signal_prepare(bool(sessions))
        return self.settings.poll_active_seconds if sessions else self.settings.poll_idle_seconds

    def _signal_prepare(self, playing: bool) -> None:
        """Preparation only runs while nothing plays; a session that just
        ended is the cue to re-read the queue (new episodes may have landed)."""
        if self.prepare is None:
            self._had_sessions = playing
            return
        session_ended = self._had_sessions and not playing
        self._had_sessions = playing
        self.prepare.set_playing(playing, session_ended=session_ended)

    def _explain_unclaimed(self, plan) -> None:
        """A silently unclaimed stream looks like a broken plugin; say why once."""
        if self.machine.claim_id is not None:
            return
        for row in plan.rows:
            reason = row.get("degraded_reason")
            if not reason:
                continue
            key = (row["session_id"], reason)
            if key in self._explained:
                continue
            self._explained.add(key)
            who = row.get("player_title") or row["session_id"]
            if reason == "user_allowlist_unconfigured":
                user = row.get("user_title") or "unknown"
                print(f"not claiming {who}: no allowed users configured, so nobody is "
                      f"eligible. If '{user}' is you, start the watcher with "
                      f"--user {user}", flush=True)
                continue
            print(f"not claiming {who}: {reason} "
                  f"(player id {row.get('player_machine_identifier')}, "
                  f"user {row.get('user_title')})", flush=True)

    def _mark_disconnected(self, conn, reason: str) -> None:
        store.set_state(conn, "watcher_state", "disconnected")
        if self.machine.claim_id:
            with conn:
                conn.execute(
                    "UPDATE session SET state='disconnected', disconnect_reason=? "
                    "WHERE session_id=?",
                    (reason[:300], self.machine.claim_id),
                )
