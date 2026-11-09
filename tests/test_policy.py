from __future__ import annotations

from watch_with_me.config import Settings
from watch_with_me.policy import evaluate, should_promote


def session(**overrides):
    base = {
        "media_type": "movie",
        "user_title": "jason",
        "player_title": "Living Room",
        "player_machine_identifier": "abc123",
        "location": "lan",
    }
    base.update(overrides)
    return base


def test_movie_on_lan_is_eligible():
    assert evaluate(session(), Settings(allowed_user_ids=["jason"])).eligible


def test_empty_user_allowlist_claims_nobody():
    decision = evaluate(session(), Settings())
    assert not decision.eligible
    assert decision.reason == "user_allowlist_unconfigured"


def test_user_not_in_allowlist_is_rejected():
    decision = evaluate(session(), Settings(allowed_user_ids=["someone_else"]))
    assert not decision.eligible
    assert decision.reason == "user_not_allowed"


def test_music_is_rejected():
    assert evaluate(session(media_type="track"), Settings()).reason == "unsupported_media_type"


def test_rewatch_is_skipped_by_default():
    assert evaluate(session(), Settings(allowed_user_ids=["jason"]),
                    known_view_count=2).reason == "already_watched"


def test_rewatch_mode_allows_seen_items():
    assert evaluate(session(), Settings(rewatch=True, allowed_user_ids=["jason"]),
                    known_view_count=2).eligible


def test_player_allowlist_filters_other_clients():
    settings = Settings(allowed_user_ids=["jason"], allowed_player_ids=["abc123"])
    assert evaluate(session(), settings).eligible
    assert evaluate(session(player_machine_identifier="zzz"), settings).reason == "player_not_allowed"


def test_player_allowlist_matches_the_client_suffixed_identifier():
    # Plex reports <machineIdentifier>-<client>, so the bare id must still match.
    settings = Settings(allowed_user_ids=["jason"], allowed_player_ids=["aa8eae38106ba773"])
    live = session(player_machine_identifier="aa8eae38106ba773-com-plexapp-android",
                   player_title="Living Room TV")
    assert evaluate(live, settings).eligible
    other = session(player_machine_identifier="9fff000000000000-com-plexapp-ios",
                    player_title="Bedroom iPad")
    assert evaluate(other, settings).reason == "player_not_allowed"


def test_player_allowlist_accepts_a_player_title():
    settings = Settings(allowed_user_ids=["jason"], allowed_player_ids=["Living Room"])
    assert evaluate(session(), settings).eligible


def test_dismissed_item_stays_dismissed_for_thirty_days():
    decision = evaluate(session(), Settings(allowed_user_ids=["jason"]),
                        seen_at=1_000_000, now=1_000_000 + 60)
    assert decision.reason == "dismissed_recently"


def test_auto_promotes_only_after_dwell():
    settings = Settings(session_start="auto", candidate_seconds=20)
    assert not should_promote("playing", 5, settings)
    assert should_promote("playing", 21, settings)
    assert not should_promote("paused", 30, settings)


def test_ask_and_manual_never_self_promote():
    assert not should_promote("playing", 60, Settings(session_start="ask"))
    assert not should_promote("playing", 60, Settings(session_start="manual"))
