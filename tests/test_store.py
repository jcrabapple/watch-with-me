from __future__ import annotations

import json

from watch_with_me import store


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "data.db"
    conn = store.connect(path)
    store.connect(path).close()  # second open must not re-apply
    versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == [1, 2, 3, 4, 5, 6, 7]
    conn.close()


def test_messages_default_to_live_reveal_state(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    with conn:
        conn.execute(
            "INSERT INTO message(id,rating_key,t_ms,kind,text,build_version) "
            "VALUES('a','1',1000,'cast','m',1)",
        )
    row = conn.execute("SELECT reveal_state FROM message WHERE id='a'").fetchone()
    assert row["reveal_state"] == "live"
    conn.close()


def test_heartbeat_marks_watcher_running(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    assert store.watcher_running(conn) is False
    store.set_state(conn, "watcher_heartbeat", "now")
    assert store.watcher_running(conn) is True
    conn.close()


def test_session_upsert_keeps_claim(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    row = {
        "session_id": "s1", "rating_key": "1", "state": "live", "claimed": 1,
        "claimed_at": 10, "player_machine_identifier": "abc", "player_title": "TV",
        "user_title": "jason", "location": "lan", "offset_ms": 1000,
        "base_offset_ms": 1000, "base_at_monotonic": 1.0, "started_at": 1,
        "last_seen_at": 1, "muted": 0, "degraded_reason": None,
        "disconnect_reason": None, "rebase_threshold_ms": 15000,
    }
    store.upsert_session(conn, row)
    store.set_state(conn, "claimed_session", "s1")
    assert json.loads(json.dumps(store.latest_session(conn)))["claimed"] == 1
    conn.close()


def test_messages_are_ordered_by_time(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    with conn:
        for mid, t in (("b", 2000), ("a", 1000)):
            conn.execute(
                "INSERT INTO message(id,rating_key,t_ms,kind,text,build_version) "
                "VALUES(?,?,?,?,?,?)", (mid, "1", t, "cast", f"m{mid}", 1),
            )
    assert [m["id"] for m in store.messages_for(conn, "1")] == ["a", "b"]
    conn.close()


def test_upsert_session_preserves_claimed_across_repeated_upserts(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    row = {
        "session_id": "s1", "rating_key": "1", "state": "live", "claimed": 1,
        "claimed_at": 10, "player_machine_identifier": "abc", "player_title": "TV",
        "user_title": "jason", "location": "lan", "offset_ms": 1000,
        "base_offset_ms": 1000, "base_at_monotonic": 1.0, "started_at": 1,
        "last_seen_at": 1, "muted": 0, "degraded_reason": None,
        "disconnect_reason": None, "rebase_threshold_ms": 15000,
    }
    store.upsert_session(conn, row)
    store.upsert_session(conn, {**row, "offset_ms": 2000, "last_seen_at": 2})
    assert store.latest_session(conn)["claimed_at"] == 10
    conn.close()


def test_delivery_ledger_is_scoped_to_the_current_item(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    with conn:
        for mid, key, t in (("m1", "100", 1000), ("m2", "200", 2000), ("m3", "100", 3000)):
            conn.execute(
                "INSERT INTO message(id,rating_key,t_ms,kind,text,build_version) "
                "VALUES(?,?,?,?,?,?)", (mid, key, t, "cast", f"note {mid}", 1),
            )
        for mid, at in (("m1", 1), ("m2", 2)):
            conn.execute(
                "INSERT INTO delivery(session_id,message_id,delivered_at,state) "
                "VALUES('s1',?,?, 'shown')", (mid, at),
            )
    # The player moved to item 200: only that item's deliveries count.
    assert store.delivered_ids(conn, "s1", "200") == {"m2"}
    assert [m["id"] for m in store.shown_messages(conn, "s1", "200")] == ["m2"]
    # The unscoped view is unchanged for callers that do not track the item.
    assert store.delivered_ids(conn, "s1") == {"m1", "m2"}
    conn.close()
