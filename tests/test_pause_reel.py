"""Pause Reel: notes accumulate silently in 'pause' mode and reveal on pause.

Rendering is JS, so these tests assert the server side: the pending-notes
endpoints (what is waiting, what marking read means), the mode plumbing in the
payloads, and that 'live' mode leaves every existing payload shape untouched.
What the page does with these payloads was verified by reading
dashboard/app.js (counter while playing, reveal on pause / counter tap,
counter hidden in 'both').
"""

from __future__ import annotations

import json

from watch_with_me import store
from watch_with_me.config import Settings

from test_api import build  # noqa: F401  (reused fixture factory)

import pytest


@pytest.fixture()
def api(tmp_path):
    return build(tmp_path / "data.db")


def _seed_session(conn, state="live", rating_key="9", session_id="s1"):
    with conn:
        conn.execute("INSERT INTO item(rating_key,media_type,title,runtime_ms) "
                     "VALUES(?,?,?,?)", (rating_key, "movie", "Something", 6000000))
        conn.execute(
            "INSERT INTO session(session_id,rating_key,state,claimed,offset_ms,"
            "base_offset_ms,base_at_monotonic,last_seen_at,rebase_threshold_ms) "
            "VALUES(?,?,?,1,5000,0,0,0,20600)", (session_id, rating_key, state))
        for i, text in enumerate(["First fact.", "Second fact.", "Third fact."], start=1):
            conn.execute(
                "INSERT INTO message(id,rating_key,t_ms,kind,text,source_name,"
                "source_url,build_version) VALUES(?,?,?,?,?,?,?,1)",
                (f"m{i}", rating_key, i * 1000, "cast", text, "Wikipedia",
                 f"https://example.test/{i}"))


def _deliver(conn, session_id="s1", message_id="m1", at=1, state="shown"):
    with conn:
        conn.execute("INSERT INTO delivery(session_id,message_id,delivered_at,state) "
                     "VALUES(?,?,?,?)", (session_id, message_id, at, state))


def test_pending_returns_notes_since_last_read_and_marks_them(api):
    module, client = api
    conn = module._conn
    _seed_session(conn)
    _deliver(conn, message_id="m1", at=1)
    _deliver(conn, message_id="m2", at=2, state="skipped")
    _deliver(conn, message_id="m3", at=3)

    body = client.get("/api/plugins/watch-with-me/notes/pending").json()
    # Newest group first; seek-skipped deliveries were never shown, so they
    # are not waiting either.
    assert [m["id"] for m in body["notes"]] == ["m3", "m1"]
    assert body["notes"][0]["t_ms"] == 3000
    assert body["notes"][0]["source_name"] == "Wikipedia"

    # The same call again: everything was marked read by the first one.
    body = client.get("/api/plugins/watch-with-me/notes/pending").json()
    assert body["notes"] == []

    # A new delivery since the read is returned exactly. (The seek-skip row
    # for m2 is promoted to shown, as the watcher would ledger it.)
    with conn:
        conn.execute("UPDATE delivery SET state='shown', delivered_at=4 "
                     "WHERE session_id='s1' AND message_id='m2'")
    body = client.get("/api/plugins/watch-with-me/notes/pending").json()
    assert [m["id"] for m in body["notes"]] == ["m2"]


def test_pending_is_scoped_to_the_sessions_current_item(api):
    module, client = api
    conn = module._conn
    _seed_session(conn, rating_key="9")
    with conn:
        conn.execute("INSERT INTO item(rating_key,media_type,title) VALUES('7','movie','Other')")
        conn.execute("INSERT INTO message(id,rating_key,t_ms,kind,text,build_version) "
                     "VALUES('old','7',1000,'cast','Old item fact.',1)")
        conn.execute("INSERT INTO delivery(session_id,message_id,delivered_at,state) "
                     "VALUES('s1','old',1,'shown')")
    _deliver(conn, message_id="m1", at=2)
    body = client.get("/api/plugins/watch-with-me/notes/pending").json()
    assert [m["id"] for m in body["notes"]] == ["m1"]


def test_pending_count_peeks_without_marking_read(api):
    module, client = api
    conn = module._conn
    _seed_session(conn)
    _deliver(conn, message_id="m1", at=1)
    _deliver(conn, message_id="m3", at=3)

    for _ in range(2):
        body = client.get("/api/plugins/watch-with-me/notes/pending/count").json()
        assert body["count"] == 2

    client.get("/api/plugins/watch-with-me/notes/pending")
    body = client.get("/api/plugins/watch-with-me/notes/pending/count").json()
    assert body["count"] == 0


def test_pending_endpoints_without_a_claimed_session_return_empty(api):
    _, client = api
    assert client.get("/api/plugins/watch-with-me/notes/pending").json()["notes"] == []
    assert client.get("/api/plugins/watch-with-me/notes/pending/count").json()["count"] == 0


def test_note_display_mode_rides_the_pending_payloads(api):
    module, client = api
    _seed_session(module._conn)
    assert client.get("/api/plugins/watch-with-me/notes/pending/count").json()["note_display"] == "live"

    store.set_state(module._conn, "settings_json", json.dumps({"note_display": "pause"}))
    assert client.get("/api/plugins/watch-with-me/notes/pending/count").json()["note_display"] == "pause"
    assert client.get("/api/plugins/watch-with-me/notes/pending").json()["note_display"] == "pause"

    # An unrecognised value degrades to the live default rather than breaking the page.
    store.set_state(module._conn, "settings_json", json.dumps({"note_display": "nonsense"}))
    assert client.get("/api/plugins/watch-with-me/notes/pending/count").json()["note_display"] == "live"


def test_settings_dataclass_accepts_the_new_key_and_defaults_to_live():
    assert Settings().note_display == "live"
    assert Settings.from_dict({"note_display": "pause"}).note_display == "pause"
    assert Settings.from_dict({"unknown_key": 1}).note_display == "live"


def test_session_payload_exposes_paused_distinctly_from_live(api):
    module, client = api
    conn = module._conn
    _seed_session(conn, state="paused")
    assert client.get("/api/plugins/watch-with-me/session").json()["state"] == "paused"

    with conn:
        conn.execute("UPDATE session SET state='live' WHERE session_id='s1'")
    assert client.get("/api/plugins/watch-with-me/session").json()["state"] == "live"


def test_live_mode_leaves_the_existing_payload_shapes_unchanged(api):
    module, client = api
    conn = module._conn
    _seed_session(conn)
    _deliver(conn, message_id="m1", at=1)

    # Default settings (no row at all) and an explicit live setting must be
    # indistinguishable on the wire (server_time_ms excepted — it is a clock).
    session_default = client.get("/api/plugins/watch-with-me/session").json()
    messages_default = client.get("/api/plugins/watch-with-me/messages").json()
    store.set_state(conn, "settings_json", json.dumps({"note_display": "live"}))
    session_live = client.get("/api/plugins/watch-with-me/session").json()
    messages_live = client.get("/api/plugins/watch-with-me/messages").json()
    assert session_live.keys() == session_default.keys()
    assert {k: v for k, v in session_live.items() if k != "server_time_ms"} == \
           {k: v for k, v in session_default.items() if k != "server_time_ms"}
    assert messages_live == messages_default

    # The shapes this page has always consumed are still exactly these.
    assert set(messages_live.keys()) == {"messages", "next_after_id", "has_more"}
    assert [m["id"] for m in messages_live["messages"]] == ["m1"]
    # The /session payload is the page's state source; it gained nothing.
    assert "note_display" not in session_live


def test_read_marks_do_not_touch_the_delivery_ledger(api):
    module, client = api
    conn = module._conn
    _seed_session(conn)
    _deliver(conn, message_id="m1", at=1)
    before = conn.execute("SELECT session_id, message_id, delivered_at, state "
                          "FROM delivery ORDER BY message_id").fetchall()
    client.get("/api/plugins/watch-with-me/notes/pending")
    after = conn.execute("SELECT session_id, message_id, delivered_at, state "
                         "FROM delivery ORDER BY message_id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
