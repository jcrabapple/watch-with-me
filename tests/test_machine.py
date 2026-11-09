from __future__ import annotations

from watch_with_me.config import Settings
from watch_with_me.machine import SessionMachine


def session(offset=0, state="playing", sid="s1", rating_key="100", **kw):
    base = {
        "session_id": sid,
        "rating_key": rating_key,
        "media_type": "movie",
        "state": state,
        "offset_ms": offset,
        "duration_ms": 6000000,
        "player_machine_identifier": "abc",
        "player_title": "TV",
        "user_title": "jason",
        "location": "lan",
    }
    base.update(kw)
    return base


def settings(**kw):
    defaults = dict(candidate_seconds=20, rebase_threshold_ms=15000, session_start="auto",
                    allowed_user_ids=["jason"])
    defaults.update(kw)
    return Settings(**defaults)


def test_candidate_then_live_after_dwell():
    m = SessionMachine(settings())
    first = m.step([session(offset=0)], now_mono=0.0, now_wall=1000)
    assert m.claim_id is None
    assert first.rows[0]["state"] == "candidate"

    second = m.step([session(offset=20000)], now_mono=21.0, now_wall=1021)
    assert m.claim_id == "s1"
    assert second.rows[0]["state"] == "live"
    assert second.rows[0]["claimed"] == 1


def test_second_stream_is_observed_and_cannot_steal_the_claim():
    m = SessionMachine(settings())
    m.step([session(offset=0)], now_mono=0.0, now_wall=1000)
    plan = m.step(
        [session(offset=20000), session(offset=0, sid="s2", rating_key="200")],
        now_mono=21.0, now_wall=1021,
    )
    assert m.claim_id == "s1"
    states = {r["session_id"]: r["state"] for r in plan.rows}
    assert states == {"s1": "live", "s2": "observed"}

    plan = m.step(
        [session(offset=40000), session(offset=30000, sid="s2", rating_key="200")],
        now_mono=45.0, now_wall=1045,
    )
    states = {r["session_id"]: r["state"] for r in plan.rows}
    assert states["s2"] == "observed"
    assert m.claim_id == "s1"


def test_pause_freezes_and_emits_nothing():
    m = SessionMachine(settings())
    m.step([session(offset=0)], now_mono=0.0, now_wall=1000)
    m.step([session(offset=20000)], now_mono=21.0, now_wall=1021)

    plan = m.step(
        [session(offset=20000, state="paused")], now_mono=40.0, now_wall=1040,
        messages=[{"id": "m1", "t_ms": 20000}],
    )
    assert plan.emit == []
    assert plan.rows[0]["state"] == "paused"


def test_a_due_message_is_emitted_once():
    m = SessionMachine(settings())
    m.step([session(offset=0)], now_mono=0.0, now_wall=1000)
    m.step([session(offset=20000)], now_mono=21.0, now_wall=1021)

    messages = [{"id": "m1", "t_ms": 25000}, {"id": "m2", "t_ms": 90000}]
    plan = m.step([session(offset=26000)], now_mono=27.0, now_wall=1027, messages=messages)
    assert [msg["id"] for msg in plan.emit] == ["m1"]

    plan = m.step([session(offset=27000)], now_mono=28.0, now_wall=1028,
                  messages=messages, delivered={"m1"})
    assert plan.emit == []


def test_forward_seek_suppresses_the_messages_it_crossed():
    m = SessionMachine(settings(candidate_seconds=5))
    m.step([session(offset=0)], now_mono=0.0, now_wall=1000)
    m.step([session(offset=10000)], now_mono=10.0, now_wall=1010)
    assert m.claim_id == "s1"

    messages = [{"id": "a", "t_ms": 25000}, {"id": "b", "t_ms": 35000},
                {"id": "c", "t_ms": 45000}]
    # Predicted 20 s, reported 40 s: a 20 s jump, above the 15 s threshold.
    plan = m.step([session(offset=40000)], now_mono=20.0, now_wall=1020, messages=messages)
    assert {msg["id"] for msg in plan.skip} == {"a", "b"}
    assert plan.emit == []


def test_backward_seek_does_not_repeat_delivered_messages():
    m = SessionMachine(settings())
    m.step([session(offset=0)], now_mono=0.0, now_wall=1000)
    m.step([session(offset=100000)], now_mono=100.0, now_wall=1100)

    messages = [{"id": "a", "t_ms": 30000}, {"id": "b", "t_ms": 90000}]
    plan = m.step([session(offset=30000)], now_mono=101.0, now_wall=1101,
                  messages=messages, delivered={"a", "b"})
    assert plan.emit == []
    assert plan.skip == []


def test_claimed_session_end_clears_the_claim_without_promoting_another():
    m = SessionMachine(settings())
    m.step([session(offset=0), session(offset=0, sid="s2", rating_key="200")],
           now_mono=0.0, now_wall=1000)
    m.step([session(offset=20000), session(offset=20000, sid="s2", rating_key="200")],
           now_mono=21.0, now_wall=1021)
    assert m.claim_id == "s1"

    plan = m.step([session(offset=30000, sid="s2", rating_key="200")],
                  now_mono=22.0, now_wall=1022)
    assert plan.ended == ["s1"]
    assert m.claim_id is None


def test_claim_is_adopted_after_a_restart():
    m = SessionMachine(settings())
    m.adopt("s1")
    plan = m.step([session(offset=5000)], now_mono=0.0, now_wall=1000)
    assert m.claim_id == "s1"
    assert plan.rows[0]["claimed"] == 1


def test_new_item_in_the_same_player_session_resets_the_clock():
    m = SessionMachine(settings(candidate_seconds=0))
    m.step([session(offset=0, rating_key="100")], now_mono=0.0, now_wall=1000)

    plan = m.step([session(offset=5000, rating_key="200")], now_mono=10.0, now_wall=1010)
    assert plan.reset == ["s1"]
    assert plan.emit == []

    plan = m.step([session(offset=6000, rating_key="200")], now_mono=11.0, now_wall=1011,
                  messages=[{"id": "m", "t_ms": 5500}])
    assert [msg["id"] for msg in plan.emit] == ["m"]


def test_a_manual_stop_suppresses_re_claiming_the_same_stream():
    m = SessionMachine(settings(candidate_seconds=0))
    m.step([session(offset=0)], now_mono=0.0, now_wall=1000, suppressed_session="s1")
    assert m.claim_id is None

    # The suppression clears once that stream is gone.
    plan = m.step([], now_mono=1.0, now_wall=1001, suppressed_session="s1")
    assert plan.clear_suppression is True


def test_dismissed_item_stays_out_of_candidates():
    m = SessionMachine(settings())
    plan = m.step([session(offset=0)], now_mono=0.0, now_wall=2_000_000,
                  policy_memory={"100": 1_900_000})
    assert plan.rows[0]["state"] == "observed"
    assert m.claim_id is None


def test_prediction_fires_a_due_message_before_the_next_heartbeat():
    # Plex heartbeats are ~10 s apart; the watcher polls every 3 s. A note due at
    # 14 s must fire from the predicted clock, not wait for the next heartbeat.
    m = SessionMachine(settings(candidate_seconds=0))
    m.step([session(offset=10_000)], now_mono=0.0, now_wall=1000)
    messages = [{"id": "m1", "t_ms": 14_000}]

    # Three polls, all reporting the same stale offset.
    first = m.step([session(offset=10_000)], now_mono=2.0, now_wall=1002, messages=messages)
    assert first.emit == []
    second = m.step([session(offset=10_000)], now_mono=4.0, now_wall=1004, messages=messages)
    assert [msg["id"] for msg in second.emit] == ["m1"]
    third = m.step([session(offset=10_000)], now_mono=6.0, now_wall=1006,
                   messages=messages, delivered={"m1"})
    assert third.emit == []


def test_stop_then_new_item_on_the_same_player_resets_the_ledger():
    m = SessionMachine(settings(candidate_seconds=0))
    m.step([session(offset=0, rating_key="100")], now_mono=0.0, now_wall=1000)
    assert m.claim_id == "s1"

    # The viewer stops: one poll sees nothing, so the claim ends.
    plan = m.step([], now_mono=1.0, now_wall=1001)
    assert plan.ended == ["s1"]
    assert m.claim_id is None

    # A new title starts on the same player; Plex reuses the session id.
    plan = m.step([session(offset=0, rating_key="200")], now_mono=2.0, now_wall=1002)
    assert plan.reset == ["s1"]


def test_a_suppressed_claim_is_ended_and_never_reclaimed():
    m = SessionMachine(settings(candidate_seconds=0))
    m.step([session(offset=0)], now_mono=0.0, now_wall=1000)
    assert m.claim_id == "s1"

    # /control end (or /watch stop) suppresses the stream while it is still playing.
    plan = m.step([session(offset=5000)], now_mono=1.0, now_wall=1001,
                  suppressed_session="s1")
    assert plan.ended == ["s1"]
    assert m.claim_id is None

    # The running watcher must not re-claim it on the following ticks.
    plan = m.step([session(offset=8000)], now_mono=2.0, now_wall=1002,
                  suppressed_session="s1")
    assert m.claim_id is None
    assert plan.rows[0]["state"] == "candidate"
