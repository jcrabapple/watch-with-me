"""Queue preparation tests: expansion, decisions, budget, playback gating,
crash recovery, and the unsupported memo — all against fakes, never the live
server (the watch-with-me-repo skill contract).

The fake client subclasses PlexClient and bypasses ``__init__`` (which does
DNS); ``_get`` routes by path prefix, the same seam test_detection uses.
"""

from __future__ import annotations

import json

from watch_with_me import queue_prep, store
from watch_with_me.config import Settings
from watch_with_me.plex import PlexClient

SRT = """1
00:00:10,000 --> 00:00:12,000
He was a good friend. ({key})

2
00:01:00,000 --> 00:01:02,000
The shield generator is down.
"""


def _hub_item(rating_key, show_key, title="An Episode", show="A Show",
              season=1, episode=1):
    return {
        "type": "episode",
        "title": title,
        "grandparentTitle": show,
        "parentTitle": f"Season {season}",
        "ratingKey": str(rating_key),
        "parentRatingKey": str(show_key + 1),
        "grandparentRatingKey": str(show_key),
        "parentIndex": season,
        "index": episode,
        "duration": 1500000,
        "key": f"/library/metadata/{rating_key}",
        "parentKey": f"/library/metadata/{show_key + 1}",
        "grandparentKey": f"/library/metadata/{show_key}",
    }


def _hub_payload(items):
    return {"MediaContainer": {"size": len(items), "Hub": [
        {"hubIdentifier": "continueWatching", "title": "Continue Watching",
         "size": len(items), "Metadata": items},
    ]}}


def _metadata(rating_key, streams, *, title="A Film", mtype="movie", year=2026):
    return {
        "ratingKey": str(rating_key),
        "type": mtype,
        "title": title,
        "year": year,
        "duration": 7_200_000,
        "Chapter": [],
        "Role": [{"tag": "Harrison Ford", "role": "Han Solo"}],
        "Media": [{"Part": [{"file": f"/media/{rating_key}.mkv", "Stream": streams}]}],
        "MediaContainer_marker": True,  # distinguishes metadata from hub payloads
    }


SIDECAR_STREAMS = [{"streamType": 3, "codec": "srt", "key": "/library/streams/9.srt"}]
IMAGE_STREAMS = [{"streamType": 3, "codec": "hdmv_pgs_subtitle"}]
NO_SUB_STREAMS = [{"streamType": 1, "codec": "h264"}]


class FakePlex(PlexClient):
    """Canned Plex: hub, per-item metadata, allLeaves, sidecar text."""

    def __init__(self, hub=None, metadata=None, leaves=None, sidecar_text=SRT):
        self.base_url = "http://plex.example:32400"
        self.token = "token"
        self.client_id = "cid"
        self.timeout = 1.0
        self._hub = hub if hub is not None else _hub_payload([])
        self._metadata = metadata or {}
        self._leaves = leaves or {}
        self._sidecar_text = sidecar_text
        self.metadata_calls = 0

    def _get(self, path, **kwargs):
        if path.startswith("/hubs/continueWatching"):
            return self._hub
        if path.startswith("/library/metadata/") and path.endswith("/allLeaves"):
            show_key = path.split("/")[3]
            return {"MediaContainer": {"Metadata": self._leaves.get(show_key, [])}}
        if path.startswith("/library/metadata/"):
            rating_key = path.split("/")[3].split("?")[0]
            self.metadata_calls += 1
            if rating_key in self._metadata:
                return {"MediaContainer": {"Metadata": [self._metadata[rating_key]]}}
            return {"MediaContainer": {"Metadata": []}}
        if path.startswith("/library/streams/"):
            if not self._sidecar_text:
                raise AssertionError("sidecar fetch for an item with no sidecar")
            return self._sidecar_text.encode()
        raise AssertionError(f"unexpected GET {path}")

    def sidecar(self, stream_key, extension="srt"):
        import re

        match = re.search(r"(\d+)", stream_key)
        key = match.group(1) if match else "0"
        # Per-item transcript text: message ids hash the anchor, so identical
        # canned SRTs for every title would collide on insert (a test-only
        # artifact; real transcripts differ).
        return self._sidecar_text.replace("{key}", key)


def sidecar_hub(*rating_keys, show_key=300):
    return FakePlex(hub=_hub_payload([
        _hub_item(key, show_key, title=f"Episode {key}") for key in rating_keys]),
        metadata={str(key): _metadata(
            key, [{"streamType": 3, "codec": "srt",
                   "key": f"/library/streams/{key}.srt"}], title=f"Episode {key}")
            for key in rating_keys})


def stub_complete(prompt: str) -> str:
    """One sourced note anchored to the first transcript line of the prompt,
    mirroring tests/test_build.py::stub_complete."""
    import re

    lines = [line for line in prompt.splitlines() if re.match(r"^\[\d+\] ", line)]
    if not lines:
        return json.dumps({"messages": []})
    anchor = lines[0].split("] ", 1)[1]
    return json.dumps({"messages": [{
        "anchor": anchor, "kind": "cast",
        "text": f"A sourced fact about {anchor}",
        "fact_ids": ["f1"], "confidence": 0.9, "subject": "han",
    }]})


def no_sentences(metadata):
    return ["Shot on location in Tunisia."]


def no_person_facts(metadata, settings):
    return []


def no_expander(settings, complete, metadata, *, wikipedia):
    return None


def make_worker(client, conn, settings, **kwargs):
    worker = queue_prep.PreparationWorker(
        settings, complete_factory=lambda: stub_complete,
        client_factory=lambda: client,
        build=lambda client, conn, settings, item, complete, *, work_dir:
            queue_prep.build_one(client, conn, settings, item, complete,
                                 work_dir=work_dir, fetch_sentences=no_sentences,
                                 fetch_person_facts=no_person_facts,
                                 make_expander=no_expander),
        **kwargs)
    worker.set_playing(False)
    return worker


# -- queue expansion by depth ---------------------------------------------------

def test_sync_off_depth_enqueues_nothing_and_never_polls(tmp_path):
    conn = store.connect(tmp_path / "data.db")

    class Exploding(FakePlex):
        def _get(self, path, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("off must not hit the network")

    report = queue_prep.sync_queue(Exploding(), conn, Settings(prepare_depth="off"))
    assert report["seen"] == 0
    assert queue_prep.queued_items(conn) == []


def test_sync_next_enqueues_the_hub_in_order(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = sidecar_hub(11, 22)
    report = queue_prep.sync_queue(client, conn, Settings(prepare_depth="next"))
    assert report["queued"] == ["11", "22"]
    assert [i["rating_key"] for i in queue_prep.queued_items(conn)] == ["11", "22"]
    seen = conn.execute("SELECT COUNT(*) AS n FROM prepare_seen").fetchone()
    assert seen["n"] == 2


def test_sync_queue_depth_covers_the_whole_hub(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = sidecar_hub(11, 22, 33, 44)
    report = queue_prep.sync_queue(client, conn, Settings(prepare_depth="queue"))
    assert report["queued"] == ["11", "22", "33", "44"]


def test_sync_two_depth_expands_the_following_episode(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    cw = _hub_item(1002, 300, title="Middle", season=2, episode=3)
    leaves = [
        {**_hub_item(1001, 300, title="S2E2", season=2, episode=2), "ratingKey": "1001"},
        {**cw, "ratingKey": "1002"},
        {**_hub_item(1003, 300, title="S2E4", season=2, episode=4), "ratingKey": "1003"},
    ]
    client = FakePlex(
        hub=_hub_payload([cw]),
        metadata={str(key): _metadata(key, SIDECAR_STREAMS, title=f"E{key}")
                  for key in (1001, 1002, 1003)},
        leaves={"300": leaves},
    )
    report = queue_prep.sync_queue(client, conn, Settings(prepare_depth="two"))
    assert report["seen"] == 2          # the CW item plus its following episode
    assert report["queued"] == ["1002", "1003"]


# -- the ready decision ----------------------------------------------------------

def test_ready_at_current_build_version_is_not_rebuilt(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = sidecar_hub(11)
    with conn:
        conn.execute(
            "INSERT INTO item(rating_key,media_type,title,build_state,build_version) "
            "VALUES('11','movie','A Film','ready',?)", (queue_prep.BUILD_VERSION,))
    report = queue_prep.sync_queue(client, conn, Settings(prepare_depth="next"))
    assert report["prepared"] == ["11"]
    assert report["queued"] == []
    assert queue_prep.queued_items(conn) == []
    # No run row: nothing was attempted.
    assert conn.execute("SELECT COUNT(*) AS n FROM prepare_run").fetchone()["n"] == 0


def test_ready_at_an_older_build_version_is_enqueued(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = sidecar_hub(11)
    with conn:
        conn.execute(
            "INSERT INTO item(rating_key,media_type,title,build_state,build_version) "
            "VALUES('11','movie','A Film','ready',1)")
    report = queue_prep.sync_queue(client, conn, Settings(prepare_depth="next"))
    assert report["queued"] == ["11"]


def test_worker_build_makes_the_item_prepared(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = sidecar_hub(11)
    settings = Settings(prepare_depth="next", prepare_budget_titles_per_day=3)
    worker = make_worker(client, conn, settings)
    assert worker.tick(conn) == "built"
    item = conn.execute("SELECT * FROM item WHERE rating_key='11'").fetchone()
    assert item["build_state"] == "ready"
    assert item["build_version"] == queue_prep.BUILD_VERSION
    run = queue_prep.latest_run(conn, "11")
    assert run["state"] == "ready"
    assert run["notes_kept"] >= 1
    assert run["duration_s"] is not None
    # A fresh sync now reports prepared and does not re-enqueue.
    report = queue_prep.sync_queue(client, conn, settings)
    assert report["prepared"] == ["11"]
    assert report["queued"] == []


# -- budget ----------------------------------------------------------------------

def test_budget_exhaustion_stops_the_run_and_reports_deferred(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = sidecar_hub(11, 22, 33)
    settings = Settings(prepare_depth="next", prepare_budget_titles_per_day=1,
                        prepare_per_run_cap=3)
    worker = make_worker(client, conn, settings)
    assert worker.tick(conn) == "built"
    assert worker.tick(conn) == "deferred"
    assert worker.last_report["stopped_reason"] == "daily_budget"
    assert sorted(worker.last_report["deferred"]) == ["22", "33"]
    # The deferred items stay queued for the next day, not dropped.
    assert sorted(i["rating_key"] for i in queue_prep.queued_items(conn)) == ["22", "33"]
    # One run row total: only the first build spent budget.
    assert queue_prep.runs_started_on(conn, queue_prep.day_key()) == 1


def test_per_run_cap_bounds_one_cycle(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = sidecar_hub(11, 22, 33)
    settings = Settings(prepare_depth="next", prepare_budget_titles_per_day=10,
                        prepare_per_run_cap=2)
    worker = make_worker(client, conn, settings)
    assert worker.tick(conn) == "built"
    assert worker.tick(conn) == "built"
    assert worker.tick(conn) == "cap_reached"
    assert [i["rating_key"] for i in queue_prep.queued_items(conn)] == ["33"]
    # A new cycle (forced sync) may build the remainder.
    worker._sync_due = 0.0
    assert worker.tick(conn) == "built"
    assert queue_prep.queued_items(conn) == []


def test_unsupported_rows_do_not_spend_budget(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = FakePlex(
        hub=_hub_payload([_hub_item(11, 300)]),
        metadata={"11": _metadata(11, NO_SUB_STREAMS)},
    )
    settings = Settings(prepare_depth="next", prepare_budget_titles_per_day=0)
    queue_prep.sync_queue(client, conn, settings)
    run = queue_prep.latest_run(conn, "11")
    assert run["state"] == "unsupported"
    assert run["remedy"]
    assert queue_prep.runs_started_on(conn, queue_prep.day_key()) == 0


# -- playback gating ---------------------------------------------------------------

def test_preparation_does_not_run_while_a_session_is_playing(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = sidecar_hub(11)
    settings = Settings(prepare_depth="next", prepare_budget_titles_per_day=3)
    worker = make_worker(client, conn, settings)
    worker.set_playing(True)
    assert worker.tick(conn) == "paused"
    assert conn.execute("SELECT COUNT(*) AS n FROM prepare_run").fetchone()["n"] == 0
    assert queue_prep.queued_items(conn) == []
    # Playback ending opens the idle window and the build proceeds.
    worker.set_playing(False, session_ended=True)
    assert worker.tick(conn) == "built"
    assert queue_prep.latest_run(conn, "11")["state"] == "ready"


# -- crash recovery -----------------------------------------------------------------

def test_crash_mid_build_is_retried_once_not_duplicated(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = sidecar_hub(11)
    settings = Settings(prepare_depth="next", prepare_budget_titles_per_day=3)

    def crashing_build(client, conn, settings, item, complete, *, work_dir):
        # Simulates a process kill mid-build: the run row is 'running', the
        # queue row is already claimed, and nothing else completes.
        queue_prep.record_run(conn, item["rating_key"])
        raise RuntimeError("simulated crash")

    worker = queue_prep.PreparationWorker(
        settings, complete_factory=lambda: stub_complete,
        client_factory=lambda: client, build=crashing_build)
    worker.set_playing(False)
    assert worker.tick(conn) == "error"
    interrupted = queue_prep.latest_run(conn, "11")
    assert interrupted["state"] == "running"
    assert queue_prep.queued_items(conn) == []  # claimed before the crash

    # --- restart ---
    assert queue_prep.recover_interrupted(conn) == 1
    recovered = queue_prep.latest_run(conn, "11")
    assert recovered["state"] == "failed"
    assert "interrupted" in recovered["error"]

    fixed = make_worker(client, conn, settings)
    assert fixed.tick(conn) == "built"
    rows = conn.execute("SELECT state FROM prepare_run WHERE rating_key='11' "
                        "ORDER BY id").fetchall()
    assert [r["state"] for r in rows] == ["failed", "ready"]
    # The retry did not double-build: one finished build, one ready item row.
    item = conn.execute("SELECT * FROM item WHERE rating_key='11'").fetchone()
    assert item["build_state"] == "ready"


def test_failed_build_marks_the_run_failed(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "data.db")
    client = sidecar_hub(11)
    settings = Settings(prepare_depth="next")

    def failing_acquire(metadata, client, settings, work_dir, conn=None):
        raise RuntimeError("transcript store unreachable")

    monkeypatch.setattr(queue_prep.build, "acquire_transcript", failing_acquire)
    outcome = queue_prep.build_one(client, conn, settings, {"rating_key": "11"},
                                   stub_complete, work_dir=tmp_path,
                                   fetch_sentences=no_sentences,
                                   fetch_person_facts=no_person_facts,
                                   make_expander=no_expander)
    assert outcome["state"] == "failed"
    run = queue_prep.latest_run(conn, "11")
    assert run["state"] == "failed"
    assert "transcript store unreachable" in run["error"]


# -- the unsupported memo ------------------------------------------------------------

def test_none_class_is_recorded_unsupported_once_with_its_remedy(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = FakePlex(
        hub=_hub_payload([_hub_item(11, 300)]),
        metadata={"11": _metadata(11, NO_SUB_STREAMS)},
    )
    settings = Settings(prepare_depth="next")
    report = queue_prep.sync_queue(client, conn, settings)
    assert report["unsupported"] == ["11"]
    assert report["queued"] == []
    run = queue_prep.latest_run(conn, "11")
    assert run["state"] == "unsupported"
    assert run["classification"] == "none"
    assert run["remedy"] == "download a subtitle in Plex"
    # Not retried every pass: a second sync writes no new row.
    queue_prep.sync_queue(client, conn, settings)
    count = conn.execute("SELECT COUNT(*) AS n FROM prepare_run WHERE rating_key='11'")
    assert count.fetchone()["n"] == 1
    # And the dashboard surfaces the remedy as the reason.
    items = queue_prep.dashboard_items(conn, settings)
    assert items[0]["state"] == "unsupported"
    assert items[0]["detail"] == "download a subtitle in Plex"


def test_image_only_with_download_enabled_is_enqueued(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = FakePlex(
        hub=_hub_payload([_hub_item(11, 300)]),
        metadata={"11": _metadata(11, IMAGE_STREAMS)},
    )
    settings = Settings(prepare_depth="next", download_subtitles=True)
    report = queue_prep.sync_queue(client, conn, settings)
    # With consent on, the build may fix the item itself: it gets a real try.
    assert report["queued"] == ["11"]
    assert queue_prep.latest_run(conn, "11") is None


# -- dashboard derivation ------------------------------------------------------------

def test_dashboard_states_cover_prepared_queued_failed_and_unsupported(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = FakePlex(
        hub=_hub_payload([
            _hub_item(11, 300, title="Built"),
            _hub_item(22, 300, title="Waiting"),
            _hub_item(33, 300, title="Broken"),
        ]),
        metadata={
            "11": _metadata(11, SIDECAR_STREAMS, title="Built"),
            "22": _metadata(22, SIDECAR_STREAMS, title="Waiting"),
            "33": _metadata(33, NO_SUB_STREAMS, title="Broken"),
        },
    )
    settings = Settings(prepare_depth="next", prepare_budget_titles_per_day=1)
    worker = make_worker(client, conn, settings)
    worker.tick(conn)              # builds 11; the day's only budget slot is spent
    # Force 22 to stay queued and 33 unsupported without building:
    worker._sync_due = 0.0
    worker.set_playing(True)       # pause before any more budget is spent
    queue_prep.sync_queue(client, conn, settings)
    by_key = {i["rating_key"]: i for i in queue_prep.dashboard_items(conn, settings)}
    assert by_key["11"]["state"] == "prepared"
    assert by_key["11"]["notes_kept"] >= 1
    assert by_key["22"]["state"] == "queued"
    assert by_key["33"]["state"] == "unsupported"

    # A failed run surfaces as failed with the error as its reason.
    run_id = queue_prep.record_run(conn, "22", now=1700000000)
    queue_prep.finish_run(conn, run_id, state="failed", error="model exploded",
                          now=1700000100)
    conn.execute("DELETE FROM prepare_queue WHERE rating_key='22'")
    conn.commit()
    by_key = {i["rating_key"]: i for i in queue_prep.dashboard_items(conn, settings)}
    assert by_key["22"]["state"] == "failed"
    assert "model exploded" in by_key["22"]["detail"]


def test_dashboard_titles_episodes_with_the_show_name(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    client = FakePlex(
        hub=_hub_payload([_hub_item(11, 300, title="The Seance", show="Harbor Lights")]),
        metadata={"11": _metadata(11, SIDECAR_STREAMS, title="The Seance",
                                  mtype="episode")},
    )
    queue_prep.sync_queue(client, conn, Settings(prepare_depth="next"))
    items = queue_prep.dashboard_items(conn, Settings())
    assert items[0]["title"] == "Harbor Lights — The Seance"
