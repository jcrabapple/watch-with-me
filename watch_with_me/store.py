"""SQLite storage. Phase 0 schema plus numbered migrations.

`connect(path)` is explicit so tests can use a temp file; production passes
nothing and resolves through Hermes' plugin_db(), which supplies WAL.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

PLUGIN_NAME = "watch-with-me"


def data_dir() -> Path:
    """Plugin data directory.

    Inside Hermes this is `plugin_data_dir()` (profile-aware, creates the dir).
    Outside it (a standalone script, a test), fall back to the same convention
    under HERMES_HOME so the two always agree on the file.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_NAME)
    except ModuleNotFoundError:
        root = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        path = root / "plugin-data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE item (
          rating_key TEXT PRIMARY KEY,
          media_type TEXT NOT NULL,
          series_key TEXT,
          title TEXT NOT NULL,
          year INTEGER,
          imdb_id TEXT,
          tmdb_id TEXT,
          tvdb_id TEXT,
          runtime_ms INTEGER,
          view_count INTEGER DEFAULT 0,
          audio_language TEXT,
          transcript_language TEXT,
          transcript_src TEXT,
          build_state TEXT DEFAULT 'none',
          build_version INTEGER DEFAULT 0,
          built_at INTEGER,
          error TEXT
        );

        CREATE TABLE message (
          id TEXT PRIMARY KEY,
          rating_key TEXT NOT NULL,
          t_ms INTEGER NOT NULL,
          kind TEXT NOT NULL,
          text TEXT NOT NULL,
          thumb_url TEXT,
          source_name TEXT,
          source_url TEXT,
          model_only INTEGER NOT NULL DEFAULT 0,
          confidence REAL,
          anchor TEXT,
          build_version INTEGER NOT NULL
        );
        CREATE INDEX message_at ON message(rating_key, t_ms);

        CREATE TABLE session (
          session_id TEXT PRIMARY KEY,
          rating_key TEXT NOT NULL,
          state TEXT NOT NULL,
          claimed INTEGER NOT NULL DEFAULT 0,
          claimed_at INTEGER,
          player_machine_identifier TEXT,
          player_title TEXT,
          user_title TEXT,
          location TEXT,
          offset_ms INTEGER NOT NULL DEFAULT 0,
          base_offset_ms INTEGER NOT NULL DEFAULT 0,
          base_at_monotonic REAL,
          started_at INTEGER,
          last_seen_at INTEGER,
          last_message_id TEXT,
          muted INTEGER NOT NULL DEFAULT 0,
          degraded_reason TEXT,
          disconnect_reason TEXT,
          rebase_threshold_ms INTEGER NOT NULL DEFAULT 15000
        );

        CREATE TABLE delivery (
          session_id TEXT NOT NULL,
          message_id TEXT NOT NULL,
          delivered_at INTEGER NOT NULL,
          PRIMARY KEY(session_id, message_id)
        );

        CREATE TABLE feedback (
          message_id TEXT NOT NULL,
          action TEXT NOT NULL,
          at INTEGER NOT NULL
        );

        CREATE TABLE policy_memory (
          rating_key TEXT PRIMARY KEY,
          action TEXT NOT NULL,
          at INTEGER NOT NULL
        );

        CREATE TABLE runtime_state (
          key TEXT PRIMARY KEY,
          value TEXT,
          updated_at INTEGER NOT NULL
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE delivery ADD COLUMN state TEXT NOT NULL DEFAULT 'shown';
        """,
    ),
    (
        3,
        """
        ALTER TABLE session ADD COLUMN media_title TEXT;
        ALTER TABLE session ADD COLUMN grandparent_title TEXT;
        ALTER TABLE session ADD COLUMN media_type TEXT;
        ALTER TABLE session ADD COLUMN duration_ms INTEGER;
        """,
    ),
    (
        4,
        """
        CREATE TABLE subtitle_cache (
          rating_key TEXT NOT NULL,
          language TEXT NOT NULL,
          path TEXT NOT NULL,
          cues INTEGER NOT NULL,
          fetched_at INTEGER NOT NULL,
          PRIMARY KEY (rating_key, language)
        );
        """,
    ),
    (
        5,
        """
        ALTER TABLE message ADD COLUMN reveal_state TEXT NOT NULL DEFAULT 'live';
        ALTER TABLE message ADD COLUMN unlock_anchor_ms INTEGER;
        ALTER TABLE message ADD COLUMN subject_key TEXT;

        CREATE TABLE claim (
          rating_key TEXT NOT NULL,
          claim_id TEXT NOT NULL,
          text TEXT NOT NULL,
          source_name TEXT,
          source_url TEXT,
          reveal_state TEXT NOT NULL,
          unlock_anchor_ms INTEGER,
          subject_key TEXT,
          model_only INTEGER NOT NULL DEFAULT 0,
          build_version INTEGER NOT NULL,
          PRIMARY KEY (rating_key, claim_id)
        );
        """,
    ),
    (
        6,
        """
        CREATE TABLE delivery_read (
          session_id TEXT NOT NULL,
          message_id TEXT NOT NULL,
          read_at INTEGER NOT NULL,
          PRIMARY KEY(session_id, message_id)
        );
        """,
    ),
    (
        7,
        """
        CREATE TABLE prepare_run (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          rating_key TEXT NOT NULL,
          state TEXT NOT NULL,
          classification TEXT,
          started_at INTEGER NOT NULL,
          finished_at INTEGER,
          duration_s REAL,
          notes_kept INTEGER,
          notes_total INTEGER,
          error TEXT,
          remedy TEXT,
          build_version INTEGER NOT NULL,
          day TEXT NOT NULL
        );
        CREATE INDEX prepare_run_key ON prepare_run(rating_key, id);
        CREATE INDEX prepare_run_day ON prepare_run(day, id);

        CREATE TABLE prepare_queue (
          rating_key TEXT PRIMARY KEY,
          position INTEGER NOT NULL,
          title TEXT,
          enqueued_at INTEGER NOT NULL
        );

        CREATE TABLE prepare_seen (
          rating_key TEXT PRIMARY KEY,
          title TEXT,
          grandparent_title TEXT,
          last_seen_at INTEGER NOT NULL
        );
        """,
    ),
]


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the database and bring the schema up to date."""
    if path is None:
        db_path = data_dir() / "data.db"
    else:
        db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (version, int(time.time())),
            )


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO runtime_state(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, int(time.time())),
        )


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def watcher_running(conn: sqlite3.Connection, stale_after_s: int = 30) -> bool:
    row = conn.execute(
        "SELECT updated_at FROM runtime_state WHERE key='watcher_heartbeat'"
    ).fetchone()
    return bool(row) and int(time.time()) - int(row["updated_at"]) <= stale_after_s


def upsert_session(conn: sqlite3.Connection, session: dict) -> None:
    # Older rows and callers predate the media columns; fill rather than fail.
    session = {"media_title": None, "grandparent_title": None, "media_type": None,
               "duration_ms": None, **session}
    with conn:
        conn.execute(
            """
            INSERT INTO session(session_id, rating_key, state, claimed, claimed_at,
                                player_machine_identifier,
                                player_title, user_title, location, offset_ms, base_offset_ms,
                                base_at_monotonic, started_at, last_seen_at, muted,
                                degraded_reason, disconnect_reason, rebase_threshold_ms,
                                media_title, grandparent_title, media_type, duration_ms)
            VALUES(:session_id,:rating_key,:state,:claimed,:claimed_at,
                   :player_machine_identifier,
                   :player_title,:user_title,:location,:offset_ms,:base_offset_ms,
                   :base_at_monotonic,:started_at,:last_seen_at,:muted,
                   :degraded_reason,:disconnect_reason,:rebase_threshold_ms,
                   :media_title,:grandparent_title,:media_type,:duration_ms)
            ON CONFLICT(session_id) DO UPDATE SET
              state=excluded.state,
              offset_ms=excluded.offset_ms,
              base_offset_ms=excluded.base_offset_ms,
              base_at_monotonic=excluded.base_at_monotonic,
              last_seen_at=excluded.last_seen_at,
              rating_key=excluded.rating_key,
              player_machine_identifier=excluded.player_machine_identifier,
              player_title=excluded.player_title,
              user_title=excluded.user_title,
              media_title=excluded.media_title,
              grandparent_title=excluded.grandparent_title,
              media_type=excluded.media_type,
              duration_ms=excluded.duration_ms,
              claimed=excluded.claimed,
              claimed_at=COALESCE(excluded.claimed_at, session.claimed_at),
              degraded_reason=excluded.degraded_reason,
              disconnect_reason=excluded.disconnect_reason
            """,
            session,
        )


def latest_session(conn: sqlite3.Connection) -> dict | None:
    # A session that already ended must never outrank a live one, and a stale
    # claimed flag on an ended row is what made the page show the previous item.
    row = conn.execute(
        "SELECT * FROM session ORDER BY (state='ended') ASC, claimed DESC, last_seen_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def messages_for(conn: sqlite3.Connection, rating_key: str, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM message WHERE rating_key=? ORDER BY t_ms LIMIT ?",
        (rating_key, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_delivery(conn: sqlite3.Connection, session_id: str, message_id: str,
                  state: str = "shown", at: int | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO delivery(session_id, message_id, delivered_at, state) "
            "VALUES(?,?,?,?)",
            (session_id, message_id, at or int(time.time()), state),
        )


def delivered_ids(conn: sqlite3.Connection, session_id: str,
                  rating_key: str | None = None) -> set[str]:
    """Delivered message ids; scoped to the session's current item when known.

    A session id belongs to a player, not a title, so a previous item's
    deliveries must not suppress the current item's notes.
    """
    if rating_key is None:
        rows = conn.execute(
            "SELECT message_id FROM delivery WHERE session_id=?", (session_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT d.message_id FROM delivery d JOIN message m ON m.id = d.message_id "
            "WHERE d.session_id=? AND m.rating_key=?", (session_id, rating_key),
        ).fetchall()
    return {r["message_id"] for r in rows}


def shown_messages(conn: sqlite3.Connection, session_id: str,
                   rating_key: str | None = None, limit: int = 40) -> list[dict]:
    """Shown notes for a session, optionally scoped to its current item."""
    if rating_key is None:
        rows = conn.execute(
            "SELECT m.*, d.delivered_at FROM delivery d JOIN message m ON m.id = d.message_id "
            "WHERE d.session_id=? AND d.state='shown' ORDER BY d.delivered_at DESC, m.t_ms DESC "
            "LIMIT ?",
            (session_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT m.*, d.delivered_at FROM delivery d JOIN message m ON m.id = d.message_id "
            "WHERE d.session_id=? AND d.state='shown' AND m.rating_key=? "
            "ORDER BY d.delivered_at DESC, m.t_ms DESC LIMIT ?",
            (session_id, rating_key, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


# -- Pause Reel: pending (delivered-but-unread) notes ----------------------------
#
# The delivery ledger itself is never touched here: read state lives in a
# separate delivery_read table keyed by (session_id, message_id). "Read" means
# the page revealed the note group to the viewer; a note is pending from the
# moment the watcher marks its delivery 'shown' until the page reveals it.

_PENDING_SQL = (
    "SELECT m.*, d.delivered_at FROM delivery d "
    "JOIN message m ON m.id = d.message_id "
    "LEFT JOIN delivery_read r ON r.session_id = d.session_id "
    "                          AND r.message_id = d.message_id "
    "WHERE d.session_id=? AND d.state='shown' AND r.message_id IS NULL {scope} "
    "ORDER BY d.delivered_at DESC, m.t_ms DESC"
)


def _pending_rows(conn: sqlite3.Connection, session_id: str,
                  rating_key: str | None = None) -> list[dict]:
    if rating_key is None:
        sql = _PENDING_SQL.format(scope="")
        rows = conn.execute(sql, (session_id,)).fetchall()
    else:
        sql = _PENDING_SQL.format(scope="AND m.rating_key=?")
        rows = conn.execute(sql, (session_id, rating_key)).fetchall()
    return [dict(r) for r in rows]


def pending_notes(conn: sqlite3.Connection, session_id: str,
                  rating_key: str | None = None, mark_read: bool = False,
                  at: int | None = None) -> list[dict]:
    """Delivered-but-unread notes, newest group first.

    With ``mark_read=True`` the returned notes are recorded as read, so a
    later call returns only what was delivered since. The delivery ledger is
    not modified.
    """
    rows = _pending_rows(conn, session_id, rating_key)
    if mark_read and rows:
        mark_notes_read(conn, session_id, [r["id"] for r in rows], at=at)
    return rows


def pending_count(conn: sqlite3.Connection, session_id: str,
                  rating_key: str | None = None) -> int:
    return len(_pending_rows(conn, session_id, rating_key))


def mark_notes_read(conn: sqlite3.Connection, session_id: str,
                    message_ids: list[str], at: int | None = None) -> None:
    with conn:
        for message_id in message_ids:
            conn.execute(
                "INSERT OR IGNORE INTO delivery_read(session_id, message_id, read_at) "
                "VALUES(?,?,?)",
                (session_id, message_id, at or int(time.time())),
            )


def timeline_for(conn: sqlite3.Connection, rating_key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM message WHERE rating_key=? ORDER BY t_ms", (rating_key,)
    ).fetchall()
    return [dict(r) for r in rows]


def withheld_claims(conn: sqlite3.Connection, rating_key: str) -> list[dict]:
    """The credits artifact: locked claims (with their unlock time) then credits.

    Locked entries sort by the time they unlock; credits-only entries (no
    unlock time) come last in fact-sheet order.
    """
    rows = conn.execute(
        "SELECT claim_id, text, source_name, source_url, reveal_state,"
        "       unlock_anchor_ms, subject_key "
        "FROM claim WHERE rating_key=? AND reveal_state IN ('locked','credits') "
        "ORDER BY CASE WHEN unlock_anchor_ms IS NULL THEN 1 ELSE 0 END,"
        "         unlock_anchor_ms, claim_id",
        (rating_key,),
    ).fetchall()
    return [dict(r) for r in rows]


def claims_unlocked_at(conn: sqlite3.Connection, rating_key: str,
                       t_ms: int) -> list[dict]:
    """Every claim safe to show at playhead `t_ms`: live claims plus locked
    claims whose subject has been established. This is the only claim set a
    live context (segment prompt, Projectionist ask) may use."""
    rows = conn.execute(
        "SELECT claim_id, text, source_name, source_url, reveal_state,"
        "       unlock_anchor_ms, subject_key "
        "FROM claim WHERE rating_key=? AND (reveal_state='locked' AND unlock_anchor_ms<=?)"
        " ORDER BY claim_id",
        (rating_key, t_ms),
    ).fetchall()
    live = conn.execute(
        "SELECT claim_id, text, source_name, source_url, reveal_state,"
        "       unlock_anchor_ms, subject_key "
        "FROM claim WHERE rating_key=? AND reveal_state='live' ORDER BY claim_id",
        (rating_key,),
    ).fetchall()
    return [dict(r) for r in live] + [dict(r) for r in rows]


def persist_claims(conn: sqlite3.Connection, rating_key: str,
                   claims: list[dict], build_version: int = 1) -> None:
    """Replace the claim ledger of one title (rebuilds supersede old state).

    Accepts both ledger shapes: classified fact-sheet claims (``id``,
    ``source``, ``url``) and withheld/artifact entries (``claim_id``,
    ``source_name``, ``source_url``).
    """
    with conn:
        conn.execute("DELETE FROM claim WHERE rating_key=?", (rating_key,))
        for entry in claims:
            conn.execute(
                "INSERT INTO claim(rating_key,claim_id,text,source_name,source_url,"
                "reveal_state,unlock_anchor_ms,subject_key,model_only,build_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    rating_key, entry.get("claim_id") or entry.get("id"),
                    entry["text"],
                    entry.get("source_name") or entry.get("source"),
                    entry.get("source_url") or entry.get("url"),
                    entry["reveal_state"], entry.get("unlock_anchor_ms"),
                    entry.get("subject_key"), int(entry.get("model_only", 0)),
                    build_version,
                ),
            )


def view_counts(conn: sqlite3.Connection, rating_keys: list[str]) -> dict[str, int]:
    if not rating_keys:
        return {}
    marks = ",".join("?" for _ in rating_keys)
    rows = conn.execute(
        f"SELECT rating_key, view_count FROM item WHERE rating_key IN ({marks})",
        rating_keys,
    ).fetchall()
    return {r["rating_key"]: int(r["view_count"] or 0) for r in rows}


def dismissals(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute("SELECT rating_key, at FROM policy_memory WHERE action='dismiss'").fetchall()
    return {r["rating_key"]: float(r["at"]) for r in rows}


def mark_session_ended(conn: sqlite3.Connection, session_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE session SET state='ended', claimed=0 WHERE session_id=?", (session_id,)
        )


def clear_deliveries(conn: sqlite3.Connection, session_id: str) -> None:
    """A session id belongs to a player, so a new item must not inherit its history."""
    with conn:
        conn.execute("DELETE FROM delivery WHERE session_id=?", (session_id,))
