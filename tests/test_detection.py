"""Detection-layer tests: Continue Watching hub parsing, the §12 preparation
taxonomy, and prewarm ordering by depth.

The hub fixture mirrors the shape verified live in
RESEARCH-PREPARATION.md §9.1 and captured (sanitised) in
artifacts/prep/continue-watching-hub-shape.json: the queue nests under
``MediaContainer.Hub[0].Metadata``, items carry ``parentRatingKey`` /
``grandparentRatingKey`` / ``parentIndex`` / ``index``, and ``viewOffset`` is
absent (the queue is "next up" entries). Show names are fictional.
"""
from __future__ import annotations

import pytest

from watch_with_me import prepare
from watch_with_me.plex import (
    PlexClient,
    classify_preparation,
    normalise_continue_watching_item,
)


def _hub_episode(rating_key, show_key, season_key, season, episode, title, show):
    return {
        "type": "episode",
        "title": title,
        "grandparentTitle": show,
        "parentTitle": f"Season {season}",
        "ratingKey": str(rating_key),
        "parentRatingKey": str(season_key),
        "grandparentRatingKey": str(show_key),
        "parentIndex": season,
        "index": episode,
        "duration": 1500000,
        "lastViewedAt": 1794022406,
        "key": f"/library/metadata/{rating_key}",
        "parentKey": f"/library/metadata/{season_key}",
        "grandparentKey": f"/library/metadata/{show_key}",
    }


#: The captured live shape: Hub[0].Metadata, no MediaContainer.Metadata,
#: no viewOffset on items.
HUB_FIXTURE = {
    "MediaContainer": {
        "size": 2,
        "identifier": "com.plexapp.plugins.library",
        "Hub": [
            {
                "hubIdentifier": "continueWatching",
                "title": "Continue Watching",
                "size": 2,
                "context": "hub.continueWatching",
                "Metadata": [
                    _hub_episode(501, 300, 301, 2, 3, "The Seance", "Harbor Lights"),
                    _hub_episode(601, 400, 401, 1, 5, "Cold Storage", "Nine Volt"),
                ],
            }
        ],
    }
}

#: The paged-endpoint shape: flat MediaContainer.Metadata with the same items.
FLAT_FIXTURE = {
    "MediaContainer": {
        "size": 2,
        "totalSize": 2,
        "offset": 0,
        "Metadata": HUB_FIXTURE["MediaContainer"]["Hub"][0]["Metadata"],
    }
}


class _StubTransportClient(PlexClient):
    """PlexClient with the HTTP layer replaced by canned payloads.

    ``__init__`` is bypassed on purpose: the real one resolves the host,
    which must never happen in a fixture-only test.
    """

    def __init__(self, payloads):
        self._payloads = payloads
        self.base_url = "http://plex.example:32400"
        self.token = "token"
        self.client_id = "cid"
        self.timeout = 1.0

    def _get(self, path, **kwargs):
        for prefix, payload in self._payloads.items():
            if path.startswith(prefix):
                return payload
        raise AssertionError(f"unexpected GET {path}")


# -- Bug 1: hub shape -------------------------------------------------------------


def test_continue_watching_parses_hub_shape():
    client = _StubTransportClient({"/hubs/continueWatching": HUB_FIXTURE})
    items = client.continue_watching()
    assert len(items) == 2
    first, second = items
    assert first["rating_key"] == "501"
    assert first["type"] == "episode"
    assert first["title"] == "The Seance"
    assert first["grandparent_title"] == "Harbor Lights"
    assert first["view_offset_ms"] == 0  # absent on the verified server
    assert first["duration_ms"] == 1500000
    assert first["parent_index"] == 2
    assert first["index"] == 3
    assert first["parent_rating_key"] == "301"
    assert first["grandparent_rating_key"] == "300"
    assert second["grandparent_rating_key"] == "400"


def test_continue_watching_parses_flat_shape():
    client = _StubTransportClient({"/hubs/continueWatching": FLAT_FIXTURE})
    items = client.continue_watching()
    assert [i["rating_key"] for i in items] == ["501", "601"]
    assert items[0]["title"] == "The Seance"


def test_continue_watching_empty_hub():
    empty = {"MediaContainer": {"size": 0, "Hub": []}}
    client = _StubTransportClient({"/hubs/continueWatching": empty})
    assert client.continue_watching() == []


def test_normalise_missing_optional_fields():
    item = normalise_continue_watching_item({"ratingKey": 7, "type": "movie"})
    assert item["rating_key"] == "7"
    assert item["view_offset_ms"] == 0
    assert item["grandparent_rating_key"] is None


# -- Bug 3: the §12 taxonomy ------------------------------------------------------


def _metadata(streams):
    return {"Media": [{"Part": [{"Stream": streams}]}]}


def test_classify_sidecar():
    result = classify_preparation(
        _metadata([{"streamType": 3, "codec": "srt", "key": "/library/streams/9.srt"}]))
    assert result["state"] == "sidecar"
    assert result["message"]
    assert result["remedy"] == "fetch the sidecar subtitle"


def test_classify_embedded():
    result = classify_preparation(
        _metadata([{"streamType": 3, "codec": "mov_text", "key": None}]))
    assert result["state"] == "embedded"
    assert "extract command" in result["remedy"]


def test_classify_embedded_when_sidecar_and_embedded_mixed():
    # Any text stream with a key means a fetchable sidecar exists.
    result = classify_preparation(_metadata([
        {"streamType": 3, "codec": "srt", "key": None},
        {"streamType": 3, "codec": "srt", "key": "/library/streams/2.srt"},
    ]))
    assert result["state"] == "sidecar"


def test_classify_image_only():
    result = classify_preparation(
        _metadata([{"streamType": 3, "codec": "hdmv_pgs_subtitle"}]))
    assert result["state"] == "image_only"
    assert "OCR" not in result["message"] or "not" in result["message"]


@pytest.mark.parametrize("codec", ["pgs", "vobsub", "dvd_subtitle"])
def test_classify_image_only_codecs(codec):
    result = classify_preparation(_metadata([{"streamType": 3, "codec": codec}]))
    assert result["state"] == "image_only"


def test_classify_none():
    result = classify_preparation(_metadata([
        {"streamType": 1, "codec": "h264"},   # video
        {"streamType": 2, "codec": "aac"},   # audio
    ]))
    assert result["state"] == "none"


def test_classify_unknown_on_incomplete_metadata():
    assert classify_preparation({})["state"] == "unknown"
    assert classify_preparation({"title": "no media key"})["state"] == "unknown"


def test_classify_never_raises():
    for metadata in ({}, {"Media": []}, {"Media": [{}]}, {"Media": [{"Part": [{}]}]}):
        result = classify_preparation(metadata)
        assert result["state"] in {"sidecar", "embedded", "image_only", "none", "unknown"}


# -- Bug 4: prewarm ordering ------------------------------------------------------


def _leaves(show_key, episodes):
    """episodes: (season_key, season, episode, title) rows."""
    return [
        {"type": "episode", "title": title, "grandparentTitle": "Show",
         "ratingKey": str(1000 + n), "parentRatingKey": str(season_key),
         "grandparentRatingKey": str(show_key), "parentIndex": season,
         "index": ep, "duration": 1400000,
         "key": f"/library/metadata/{1000 + n}",
         "parentKey": f"/library/metadata/{season_key}",
         "grandparentKey": f"/library/metadata/{show_key}"}
        for n, (season_key, season, ep, title) in enumerate(episodes)
    ]


def _cw(show_key, season_key, rating_key, season, ep, title="Now Watching"):
    item = normalise_continue_watching_item(
        _hub_episode(rating_key, show_key, season_key, season, ep, title, "Show"))
    return item


def test_prewarm_off_returns_nothing():
    assert prepare.prewarm_items([_cw(1, 2, 3, 1, 1)], "off") == []
    assert prepare.prewarm_items([_cw(1, 2, 3, 1, 1)], "off",
                                 leaves_lookup=lambda k: []) == []


def test_prewarm_next_returns_hub_order():
    items = [_cw(1, 11, 101, 1, 2), _cw(2, 21, 201, 3, 1), _cw(1, 12, 102, 1, 3)]
    result = prepare.prewarm_items(items, "next")
    assert [i["rating_key"] for i in result] == ["101", "201", "102"]


def test_prewarm_queue_returns_whole_hub():
    items = [_cw(1, 11, 101, 1, 2), _cw(2, 21, 201, 3, 1)]
    result = prepare.prewarm_items(items, "queue")
    assert [i["rating_key"] for i in result] == ["101", "201"]


def test_prewarm_next_dedupes_same_show_reentries():
    items = [_cw(1, 11, 101, 1, 2), _cw(1, 12, 102, 1, 3)]
    result = prepare.prewarm_items(items, "next")
    assert [i["rating_key"] for i in result] == ["101", "102"]


def test_prewarm_two_expands_one_following_episode():
    cw = _cw(300, 301, 1002, 2, 3, "Middle")  # rating key matches the leaf below
    leaves = _leaves(300, [(301, 2, 1, "S2E1"), (301, 2, 2, "S2E2"),
                           (301, 2, 3, "Middle"), (301, 2, 4, "S2E4")])
    result = prepare.prewarm_items([cw], "two", leaves_lookup=lambda k: leaves)
    assert [i["rating_key"] for i in result] == ["1002", "1003"]  # Middle, then S2E4


def test_prewarm_two_stops_at_end_of_show():
    cw = _cw(300, 302, 1003, 2, 4, "Finale")
    leaves = _leaves(300, [(301, 2, 1, "S2E1"), (301, 2, 2, "S2E2"),
                           (302, 2, 3, "S2E3"), (302, 2, 4, "Finale")])
    result = prepare.prewarm_items([cw], "two", leaves_lookup=lambda k: leaves)
    assert [i["rating_key"] for i in result] == ["1003"]


def test_prewarm_two_crosses_season_boundary():
    cw = _cw(300, 301, 1002, 1, 3, "S1 Finale")
    leaves = _leaves(300, [(301, 1, 1, "S1E1"), (301, 1, 2, "S1E2"),
                           (301, 1, 3, "S1 Finale"),
                           (302, 2, 1, "S2E1"), (302, 2, 2, "S2E2")])
    result = prepare.prewarm_items([cw], "two", leaves_lookup=lambda k: leaves)
    assert [i["title"] for i in result] == ["S1 Finale", "S2E1"]


def test_prewarm_two_one_episode_before_season_end():
    # The current episode is second-to-last of the season: exactly one
    # episode sits between it and the season end, and that one is expanded.
    cw = _cw(300, 301, 1001, 1, 2, "Setup")
    leaves = _leaves(300, [(301, 1, 1, "S1E1"), (301, 1, 2, "Setup"),
                           (301, 1, 3, "Payoff")])
    result = prepare.prewarm_items([cw], "two", leaves_lookup=lambda k: leaves)
    assert [i["title"] for i in result] == ["Setup", "Payoff"]


def test_prewarm_two_caches_leaves_per_show():
    cw_a = _cw(300, 301, 1000, 1, 1, "A")
    cw_b = _cw(300, 302, 1001, 2, 1, "B")
    leaves = _leaves(300, [(301, 1, 1, "A"), (302, 2, 1, "B")])
    calls = []

    def lookup(show_key):
        calls.append(show_key)
        return leaves

    prepare.prewarm_items([cw_a, cw_b], "two", leaves_lookup=lookup)
    assert calls == ["300"]


def test_prewarm_two_without_lookup_degrades_to_queue():
    cw = _cw(300, 301, 1500, 1, 1, "A")
    result = prepare.prewarm_items([cw], "two")
    assert [i["rating_key"] for i in result] == ["1500"]


def test_prewarm_rejects_unknown_depth():
    with pytest.raises(ValueError):
        prepare.prewarm_items([], "shelf")


def test_collect_prewarm_queue_off_never_polls():
    class Exploding:
        def continue_watching(self):  # pragma: no cover - must not be called
            raise AssertionError("off must not hit the network")

    assert prepare.collect_prewarm_queue(Exploding(), "off") == []


def test_collect_prewarm_queue_two_uses_client_leaves():
    leaves_payload = {
        "MediaContainer": {
            "Metadata": [
                {"type": "episode", "title": "E1", "ratingKey": "1500",
                 "grandparentRatingKey": "300", "parentRatingKey": "301",
                 "parentIndex": 1, "index": 1},
                {"type": "episode", "title": "E2", "ratingKey": "1501",
                 "grandparentRatingKey": "300", "parentRatingKey": "301",
                 "parentIndex": 1, "index": 2},
            ]
        }
    }
    client = _StubTransportClient({
        "/hubs/continueWatching": HUB_FIXTURE,
        "/library/metadata/300/allLeaves": leaves_payload,
        "/library/metadata/400/allLeaves": {"MediaContainer": {"Metadata": []}},
    })
    result = prepare.collect_prewarm_queue(client, "two")
    # Hub order: Harbor Lights (show 300), then Nine Volt (show 400, no leaves
    # match → unexpanded). Show 300's CW item 501 (S2E3) is not in these
    # stubbed leaves, so it also does not expand.
    assert [i["rating_key"] for i in result][:2] == ["501", "601"]
    assert len(result) == 2
