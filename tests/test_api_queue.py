"""The /queue dashboard endpoint: one line per Continue Watching item with
the four user-visible states (prepared | queued | unsupported | failed) plus
the daily budget counter."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import build  # noqa: E402


def test_queue_endpoint_reports_states_and_budget(tmp_path):
    module, client = build(tmp_path / "data.db")
    conn = module._conn
    from watch_with_me import queue_prep

    with conn:
        conn.execute(
            "INSERT INTO prepare_seen(rating_key,title,grandparent_title,last_seen_at) "
            "VALUES('11','Built',NULL,1700000000)")
        conn.execute(
            "INSERT INTO prepare_seen(rating_key,title,grandparent_title,last_seen_at) "
            "VALUES('22','Waiting',NULL,1700000001)")
        conn.execute(
            "INSERT INTO prepare_seen(rating_key,title,grandparent_title,last_seen_at) "
            "VALUES('33','Broken',NULL,1700000002)")
        conn.execute(
            "INSERT INTO item(rating_key,media_type,title,build_state,build_version) "
            "VALUES('11','movie','Built','ready',?)", (queue_prep.BUILD_VERSION,))
        conn.execute(
            "INSERT INTO prepare_queue(rating_key,position,title,enqueued_at) "
            "VALUES('22',0,'Waiting',1700000003)")
    run_id = queue_prep.record_run(conn, "33", state="unsupported",
                                   classification="none", now=1700000004,
                                   remedy="download a subtitle in Plex")
    assert run_id

    body = client.get("/api/plugins/watch-with-me/queue").json()
    assert body["depth"] == "off"  # Settings default
    assert body["budget"]["limit"] == 3
    assert body["budget"]["used"] == 0  # unsupported rows spend no budget
    by_key = {i["rating_key"]: i for i in body["items"]}
    assert by_key["11"]["state"] == "prepared"
    assert by_key["22"]["state"] == "queued"
    assert by_key["33"]["state"] == "unsupported"
    assert by_key["33"]["detail"] == "download a subtitle in Plex"


def test_queue_endpoint_is_empty_without_a_hub_snapshot(tmp_path):
    _, client = build(tmp_path / "data.db")
    body = client.get("/api/plugins/watch-with-me/queue").json()
    assert body["items"] == []
    assert body["budget"]["used"] == 0
