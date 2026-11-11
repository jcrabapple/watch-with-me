"""Background watcher: poll Plex, drive the session machine, persist the result.

One thread per process, guarded by a file lock so a gateway process and a
plugin-host child can never both poll. The router reads the database and the
heartbeat, so it does not need to be in the same process as the watcher.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import threading
import time
from pathlib import Path

from . import onboarding
from . import models
from . import queue_prep
from . import store
from .config import Settings
from .machine import CLAIMED_SESSION_KEY, SessionMachine
from .plex import PlexClient, PlexError

log = logging.getLogger(__name__)

HEARTBEAT = "watcher_heartbeat"
CREDENTIALS = onboarding.CREDENTIALS

#: Runtime settings overlay keys written by the dashboard UI. ``settings_json``
#: in runtime_state holds ONLY these keys (sparse overrides); older builds
#: stored a full startup snapshot there, which masked Settings → Plugins edits.
UI_OVERLAY_KEYS = frozenset({
    "allowed_user_ids", "allowed_player_ids", "note_display",
    "rewatch", "prepare_depth", "public_base_url",
})

#: Notification dedup persistence: (session_id, rating_key, mode) triples,
#: newest last, bounded to the most recent NOTIFIED_CLAIMS_MAX.
NOTIFIED_CLAIMS_KEY = "notified_claims_json"
NOTIFIED_CLAIMS_MAX = 50


def credentials_path() -> Path:
    # The one writer lives in onboarding; this indirection keeps the tests'
    # monkeypatch of watcher_mod.credentials_path effective.
    return onboarding.credentials_path()


def load_credentials() -> dict | None:
    return onboarding.load_credentials(path=credentials_path())


class Watcher(threading.Thread):
    def __init__(self, settings: Settings | None = None, *,
                 prepare_worker: queue_prep.PreparationWorker | None = None,
                 notify=None) -> None:
        super().__init__(name="watch-with-me-watcher", daemon=True)
        self.settings = settings or Settings()
        self.machine = SessionMachine(self.settings)
        # The preparation worker is owned here but driven by signals: the
        # watcher knows when something plays; the worker decides when to
        # build. plugin.py constructs it (it needs the host LLM factory).
        self.prepare = prepare_worker
        # Chat handoff callback (plugin.py builds it around ctx.inject_message).
        # Fires at most once per (session_id, rating_key): on a new claim, or
        # when a session enters candidate under session_start == "ask".
        self.notify = notify
        # Lazy-loaded from NOTIFIED_CLAIMS_KEY on first use so a watcher
        # restart never re-notifies a session that already got its message.
        self._notified: list[tuple[str, str, str]] | None = None
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

    def _effective_settings(self, conn, creds: dict) -> Settings:
        """Settings for this poll: live host config plus the sparse
        ``settings_json`` overlay (written by the dashboard UI) over the
        startup settings.

        Re-read on every poll so a Settings → Plugins edit or a one-tap page
        action applies on the next tick without restarting the watcher. The
        overlay only carries the specific keys the UI changed, so a later
        Settings → Plugins edit to any other key still lands. The linked Plex
        account's username (and display title, when it differs) authorizes the
        account owner when no allowed users are configured; pure
        ``policy.evaluate()`` keeps its strict empty-allowlist contract, so the
        fallback lives here only.
        """
        base = self.settings.as_dict()
        try:
            base.update(models.plugin_settings(models.load_hermes_config()))
        except Exception:  # noqa: BLE001 - a config read glitch must not kill the poll
            pass
        raw = store.get_state(conn, "settings_json", "") or ""
        try:
            overlay = json.loads(raw) or {}
        except ValueError:
            overlay = {}
        if isinstance(overlay, dict):
            base.update(overlay)
        effective = Settings.from_dict(base)
        if not effective.allowed_user_ids:
            names: list[str] = []
            for value in (creds.get("username"), creds.get("account_title")):
                if value and value not in names:
                    names.append(value)
            if names:
                effective.allowed_user_ids = names
        return effective

    def _poll_once(self, conn, creds: dict) -> float:
        effective = self._effective_settings(conn, creds)
        self.machine.settings = effective
        client = PlexClient(
            creds["base_url"], creds["token"], creds.get("client_id", "watch-with-me"),
            allow_loopback=effective.allow_loopback_plex,
        )
        sessions = client.sessions()
        self._failures = 0

        if not self._adopted:
            # Restart recovery: resume the claim the database remembers even
            # when the session is absent from this snapshot (it may return on
            # a later poll).
            self.machine.adopt(store.get_state(conn, CLAIMED_SESSION_KEY, "") or None)
            self._adopted = True
        else:
            # One-tap page claim (POST /session/claim) while this watcher is
            # running: the claim lands in runtime_state, and adopting it here
            # — every poll — is what keeps the machine from overwriting
            # claimed=0 on the very next tick.
            stored_claim = store.get_state(conn, CLAIMED_SESSION_KEY, "") or None
            suppressed = store.get_state(conn, "suppressed_session", "") or None
            if (stored_claim and stored_claim != suppressed
                    and self.machine.claim_id != stored_claim):
                live = next((s for s in sessions if s["session_id"] == stored_claim), None)
                if live is not None:
                    self.machine.adopt(stored_claim)
                    self.machine.bases.setdefault(
                        stored_claim, (live["offset_ms"], time.monotonic()))

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
        self._notify_claims(conn, plan, effective)
        self._signal_prepare(bool(sessions))
        return self.settings.poll_active_seconds if sessions else self.settings.poll_idle_seconds

    def _load_notified(self, conn) -> list[tuple[str, str, str]]:
        """The persisted (session_id, rating_key, mode) triples, newest last.

        Loaded once per Watcher from SQLite so a restart does not re-notify;
        the in-memory copy is the fast path for the rest of the process life.
        """
        if self._notified is not None:
            return self._notified
        keys: list[tuple[str, str, str]] = []
        raw = store.get_state(conn, NOTIFIED_CLAIMS_KEY, "") or ""
        try:
            data = json.loads(raw) or []
        except ValueError:
            data = []
        if isinstance(data, list):
            for entry in data[-NOTIFIED_CLAIMS_MAX:]:
                if isinstance(entry, (list, tuple)) and len(entry) == 3:
                    keys.append((str(entry[0]), str(entry[1]), str(entry[2])))
        self._notified = keys
        return keys

    def _persist_notified(self, conn, keys: list[tuple[str, str, str]]) -> None:
        self._notified = keys
        store.set_state(conn, NOTIFIED_CLAIMS_KEY,
                        json.dumps([list(key) for key in keys]))

    def _notify_claims(self, conn, plan, settings: Settings) -> None:
        """The chat handoff: one notification per (session_id, rating_key, mode).

        "claimed" fires when the machine newly owns a stream; "ask" fires when
        an eligible stream sits unclaimed under session_start == "ask" and the
        viewer may want to start it from chat. The note count is read from the
        store because the claiming poll itself has not loaded a timeline yet.
        A triple is recorded (and persisted, bounded) ONLY when the callback
        reports delivery: a denied injection or a raising gateway stays
        eligible, so enabling the feature later retries instead of having been
        permanently swallowed, and a watcher restart never double-notifies.
        """
        if self.notify is None or not settings.chat_notifications:
            return
        notified = self._load_notified(conn)
        ask_mode = settings.session_start == "ask"
        for row in plan.rows:
            if row["claimed"]:
                mode = "claimed"
                notes_count = len(store.timeline_for(conn, row["rating_key"]))
            elif ask_mode and row["state"] == "candidate":
                mode = "ask"
                notes_count = 0
            else:
                continue
            key = (row["session_id"], row["rating_key"], mode)
            if key in notified:
                continue
            try:
                delivered = bool(self.notify(row, notes_count, mode))
            except Exception:  # noqa: BLE001 - a refused message must not kill the poll
                log.exception("watch-with-me: chat notification failed")
                continue
            if delivered:
                notified.append(key)
                self._persist_notified(conn, notified[-NOTIFIED_CLAIMS_MAX:])

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
