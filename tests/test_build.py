from __future__ import annotations

import json

from watch_with_me import build, store
from watch_with_me.config import Settings

SRT = """1
00:00:10,000 --> 00:00:12,000
He was a good friend.

2
00:01:00,000 --> 00:01:02,000
The shield generator is down.
"""


def metadata(**kw):
    base = {
        "ratingKey": "42",
        "type": "movie",
        "title": "Test Feature",
        "year": 2026,
        "duration": 7_200_000,
        "Chapter": [],
        "Role": [{"tag": "Harrison Ford", "role": "Han Solo"}],
        "Media": [{"Part": [{"file": "/media/test.mkv", "Stream": [
            {"streamType": 3, "codec": "srt", "key": "/library/streams/999"}
        ]}]}],
    }
    base.update(kw)
    return base


class FakeClient:
    def __init__(self, text=SRT):
        self.text = text
        self.calls = []

    def sidecar(self, key, ext="srt"):
        self.calls.append(key)
        if not self.text:
            raise RuntimeError("501 not a sidecar")
        return self.text


def stub_complete(prompt: str) -> str:
    import re

    lines = [line for line in prompt.splitlines() if re.match(r"^\[\d+\] ", line)]
    if not lines:
        return json.dumps({"messages": []})
    anchor = lines[0].split("] ", 1)[1]
    return json.dumps({"messages": [{
        "anchor": anchor, "kind": "cast", "text": "A sourced fact.",
        "fact_ids": ["f1"], "confidence": 0.9, "subject": "han",
    }]})


def test_acquire_transcript_prefers_the_sidecar(tmp_path):
    client = FakeClient()
    text, source = build.acquire_transcript(metadata(), client, Settings(), tmp_path)
    assert source == "sidecar"
    assert "good friend" in text
    assert client.calls == ["/library/streams/999"]


def test_acquire_transcript_falls_back_to_the_extractor(tmp_path):
    source_file = tmp_path / "media.srt"
    source_file.write_text(SRT, encoding="utf-8")
    client = FakeClient(text="")  # sidecar raises

    text, source = build.acquire_transcript(
        metadata(Media=[{"Part": [{"file": str(source_file), "Stream": []}]}]),
        client,
        Settings(extract_command="cp {media_path} {output}"),
        tmp_path,
    )
    assert source == "extractor"
    assert "good friend" in text


def test_acquire_transcript_reports_none_when_nothing_works(tmp_path):
    text, source = build.acquire_transcript(
        metadata(Media=[{"Part": [{"file": "/nope.mkv", "Stream": []}]}]),
        FakeClient(text=""), Settings(), tmp_path,
    )
    assert (text, source) == ("", "none")


def test_build_messages_produces_anchored_messages():
    result = build.build_messages(metadata(), SRT, ["Shot on location in Tunisia."],
                                  stub_complete)
    assert result.state == "ready"
    assert result.segments >= 1
    assert result.messages
    assert all(m["t_ms"] >= 0 for m in result.messages)
    assert result.stats["anchor_hit_rate"] > 0


def test_build_messages_degrades_without_a_transcript():
    result = build.build_messages(metadata(), "", [], stub_complete)
    assert result.state == "degraded"
    assert result.error == "no_transcript"
    assert result.messages == []


def test_persist_writes_item_and_messages(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    # The wikipedia sentence keeps a live fact in the sheet: with only Plex
    # cast facts, the stub's citation would be credits-quarantined and the
    # build would degrade (that path is covered in test_spoilers).
    result = build.build_messages(metadata(), SRT,
                                  ["Shot on location in Tunisia."], stub_complete)
    result.transcript_src = "sidecar"
    build.persist(conn, result, metadata())

    item = conn.execute("SELECT * FROM item WHERE rating_key='42'").fetchone()
    assert item["build_state"] == "ready"
    assert item["transcript_src"] == "sidecar"
    stored = store.timeline_for(conn, "42")
    assert len(stored) == len(result.messages)
    conn.close()
