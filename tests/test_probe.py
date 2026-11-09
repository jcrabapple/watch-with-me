from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import probe_plex  # noqa: E402


def movie(chapters=0, sidecar=False, embedded=True):
    streams = []
    if sidecar:
        streams.append({"streamType": 3, "codec": "srt", "key": "/library/streams/1.srt"})
    if embedded:
        streams.append({"streamType": 3, "codec": "srt", "key": None})
    return {
        "title": "The Cycle",
        "year": 2026,
        "Guid": [{"id": "imdb://tt32358390"}, {"id": "tmdb://1290418"}],
        "Chapter": [{"index": i} for i in range(chapters)],
        "Media": [{"Part": [{"Stream": streams}]}],
    }


def test_embedded_only_falls_back_to_dialogue_gaps():
    result = probe_plex.classify_title(movie())
    assert result["segmentation"] == "dialogue_gaps"
    assert result["transcript"] == "embedded_only"
    assert result["timed_cues_possible"] is True


def test_sidecar_needs_no_fallback():
    result = probe_plex.classify_title(movie(sidecar=True))
    assert result["transcript"] == "sidecar"
    assert result["sidecar_subtitles"] == 1


def test_chapters_win_when_present():
    result = probe_plex.classify_title(movie(chapters=12))
    assert result["segmentation"] == "chapters"


def test_no_subtitles_means_fixed_windows_and_no_timed_cues():
    result = probe_plex.classify_title(movie(embedded=False))
    assert result["segmentation"] == "fixed_windows"
    assert result["timed_cues_possible"] is False
