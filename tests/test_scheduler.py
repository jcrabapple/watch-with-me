from __future__ import annotations

from watch_with_me import scheduler


def test_frozen_when_not_playing():
    assert scheduler.predicted_offset_ms(1000, 10.0, 20.0, "paused") == 1000


def test_predicts_forward_when_playing():
    assert scheduler.predicted_offset_ms(1000, 10.0, 12.5, "playing") == 3500


def test_small_drift_is_reporting_lag_not_a_seek():
    assert not scheduler.is_discontinuity(10_000, 12_000, threshold_ms=15_000)


def test_large_drift_is_a_seek():
    assert scheduler.is_discontinuity(10_000, 40_000, threshold_ms=15_000)


def test_default_rebase_threshold_matches_the_measured_web_cadence():
    # Phase 0 measured p95 = 10.3 s on Plex Web; 2x that cadence is 20.6 s.
    from watch_with_me.config import Settings

    assert Settings().rebase_threshold_ms == 20_600


def test_a_normal_cadence_update_is_not_a_seek():
    # Rebased at 100 s, ten seconds later the client reports 110 s.
    assert scheduler.classify_sample(110_000, 110_000, 20_600) == "update"


def test_a_forward_jump_is_a_seek():
    assert scheduler.classify_sample(100_000, 400_000, 20_600) == "seek"


def test_only_one_message_is_due_per_pass():
    messages = [{"id": "a", "t_ms": 1000}, {"id": "b", "t_ms": 2000}, {"id": "c", "t_ms": 9000}]
    due = scheduler.due_messages(messages, 5000, set())
    assert [m["id"] for m in due] == ["a"]


def test_delivered_messages_are_not_repeated():
    messages = [{"id": "a", "t_ms": 1000}, {"id": "b", "t_ms": 2000}]
    due = scheduler.due_messages(messages, 5000, {"a"})
    assert [m["id"] for m in due] == ["b"]
