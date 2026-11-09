"""The session state machine, with no I/O.

`Watcher` supplies snapshots and persists the result; everything that decides
what happens lives here, so it can be tested by feeding it session lists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import scheduler
from .config import Settings
from .policy import evaluate, should_promote

CLAIMED_SESSION_KEY = "claimed_session"


@dataclass
class Plan:
    rows: list[dict] = field(default_factory=list)
    emit: list[dict] = field(default_factory=list)
    skip: list[dict] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)
    reset: list[str] = field(default_factory=list)
    clear_suppression: bool = False
    watcher_state: str = "idle"


class SessionMachine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.first_seen: dict[str, float] = {}
        self.claim_id: str | None = None
        self.bases: dict[str, tuple[int, float]] = {}
        self.items: dict[str, str] = {}

    def adopt(self, stored_claim: str | None) -> None:
        """Restart recovery: resume the claim the database remembers."""
        if stored_claim:
            self.claim_id = stored_claim

    def step(
        self,
        sessions: list[dict],
        *,
        now_mono: float,
        messages: list[dict] | None = None,
        delivered: set[str] | None = None,
        view_counts: dict[str, int] | None = None,
        policy_memory: dict[str, float] | None = None,
        now_wall: float | None = None,
        suppressed_session: str | None = None,
    ) -> Plan:
        messages = messages or []
        delivered = delivered or set()
        view_counts = view_counts or {}
        policy_memory = policy_memory or {}
        plan = Plan()
        seen: set[str] = set()

        for session in sessions:
            sid = session["session_id"]
            seen.add(sid)
            self.first_seen.setdefault(sid, now_mono)

        if self.claim_id and self.claim_id == suppressed_session:
            # An explicit end (/control, /watch stop) must drop a live claim,
            # not just prevent a future one; the stream may still be playing.
            plan.ended.append(self.claim_id)
            self.bases.pop(self.claim_id, None)
            self.claim_id = None
            for other in seen:
                self.first_seen[other] = now_mono

        if self.claim_id and self.claim_id not in seen:
            plan.ended.append(self.claim_id)
            self.bases.pop(self.claim_id, None)
            # Keep self.items: a stop-then-start on the same player must still
            # be recognised as a new item so the delivery ledger resets.
            self.claim_id = None
            # A claim that ends does not hand over to another stream: every
            # remaining session restarts its dwell, so promotion is deliberate.
            for other in seen:
                self.first_seen[other] = now_mono

        if suppressed_session and suppressed_session not in seen:
            plan.clear_suppression = True

        if self.claim_id is None and not plan.ended:
            self.claim_id = self._choose_claim(sessions, view_counts, policy_memory,
                                               now_mono, now_wall,
                                               suppressed_session=suppressed_session)

        for session in sessions:
            sid = session["session_id"]
            plan.rows.append(self._row(session, sid, now_mono, now_wall,
                                       view_counts, policy_memory))

        claimed = next((s for s in sessions if s["session_id"] == self.claim_id), None)
        if claimed is not None:
            sid = claimed["session_id"]
            rating_key = claimed["rating_key"]
            previous_item = self.items.get(sid)
            if previous_item is not None and previous_item != rating_key:
                # Same player, new item: reset the clock and forget what was shown.
                plan.reset.append(sid)
                self.bases[sid] = (claimed["offset_ms"], now_mono)
                self.items[sid] = rating_key
            else:
                self.items[sid] = rating_key
                plan.emit, plan.skip = self._advance(claimed, messages, delivered, now_mono)

        for sid in list(self.first_seen):
            if sid not in seen:
                self.first_seen.pop(sid, None)

        plan.watcher_state = "active" if sessions else "idle"
        return plan

    # -- internals ---------------------------------------------------------
    def _choose_claim(self, sessions, view_counts, policy_memory, now_mono, now_wall,
                      suppressed_session: str | None = None) -> str | None:
        for session in sessions:
            sid = session["session_id"]
            if sid == suppressed_session:
                continue
            decision = evaluate(
                session,
                self.settings,
                known_view_count=view_counts.get(session["rating_key"], 0),
                seen_at=policy_memory.get(session["rating_key"]),
                now=now_wall,
            )
            if not decision.eligible:
                continue
            dwell = now_mono - self.first_seen[sid]
            if should_promote(session["state"], dwell, self.settings):
                self.bases.setdefault(sid, (session["offset_ms"], now_mono))
                return sid
        return None

    def _row(self, session, sid, now_mono, now_wall, view_counts, policy_memory) -> dict:
        if sid == self.claim_id:
            state = "paused" if session["state"] in ("paused", "buffering") else "live"
            reason = None
        else:
            decision = evaluate(
                session,
                self.settings,
                known_view_count=view_counts.get(session["rating_key"], 0),
                seen_at=policy_memory.get(session["rating_key"]),
                now=now_wall,
            )
            # Another stream owns the claim; this one is only observed.
            state = "observed" if self.claim_id is not None else (
                "candidate" if decision.eligible else "observed")
            reason = None if decision.eligible else decision.reason
        base_offset, base_at = self.bases.get(sid, (session["offset_ms"], now_mono))
        return {
            "session_id": sid,
            "rating_key": session["rating_key"],
            "state": state,
            "claimed": 1 if sid == self.claim_id else 0,
            "claimed_at": int(now_wall) if sid == self.claim_id and now_wall else None,
            "player_machine_identifier": session.get("player_machine_identifier"),
            "player_title": session.get("player_title"),
            "user_title": session.get("user_title"),
            "location": session.get("location"),
            "offset_ms": session["offset_ms"],
            "base_offset_ms": base_offset,
            "base_at_monotonic": base_at,
            "started_at": int(now_wall) if now_wall else None,
            "last_seen_at": int(now_wall) if now_wall else None,
            "muted": 0,
            "degraded_reason": reason if sid != self.claim_id else None,
            "media_title": session.get("title"),
            "grandparent_title": session.get("grandparent_title"),
            "media_type": session.get("media_type"),
            "duration_ms": session.get("duration_ms"),
            "disconnect_reason": None,
            "rebase_threshold_ms": self.settings.rebase_threshold_ms,
        }

    def _advance(self, session, messages, delivered, now_mono) -> tuple[list[dict], list[dict]]:
        sid = session["session_id"]
        if session["state"] != "playing":
            self.bases[sid] = (session["offset_ms"], now_mono)
            return [], []

        base_offset, base_at = self.bases.get(sid, (session["offset_ms"], now_mono))
        predicted = scheduler.predicted_offset_ms(base_offset, base_at, now_mono, "playing")
        reported = session["offset_ms"]
        verdict = scheduler.classify_sample(predicted, reported, self.settings.rebase_threshold_ms)

        skip: list[dict] = []
        if verdict == "seek":
            skip = scheduler.skipped_by_seek(messages, predicted, reported, delivered)
            self.bases[sid] = (reported, now_mono)
        elif reported != base_offset:
            # A genuinely new heartbeat: re-anchor to the fresh report. When the
            # report is unchanged, keep the old base so elapsed monotonic time
            # keeps predicting forward between heartbeats.
            self.bases[sid] = (reported, now_mono)

        due = scheduler.due_messages(messages, predicted, delivered | {m["id"] for m in skip})
        return due, skip
