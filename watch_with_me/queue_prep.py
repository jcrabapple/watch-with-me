"""Continue Watching preparation: the queue, the budget, and the builds.

This is the "prep" loop the plugin runs in the background so a viewer never
starts an unprepared title. It lives in one daemon thread
(``PreparationWorker``) owned by the Watcher: the watcher reports
playing/idle, the worker re-reads the Continue Watching hub at a slow
cadence, decides what needs building, and builds it — one title at a time,
never during playback, never past the daily budget.

Persistence contract (a restart must retry, never duplicate):

- ``prepare_seen`` — every item in the last hub snapshot (dashboard source).
- ``prepare_queue`` — the bounded work list for the current cycle; claiming
  deletes the row, so a crash between claim and finish cannot double-build.
- ``prepare_run`` — one row per build attempt, written with state ``running``
  *before* the build starts. A crash leaves that row behind;
  :func:`recover_interrupted` (run at worker start) marks it ``failed`` with
  error ``interrupted``, and the next sync re-enqueues the item because its
  ``item.build_state`` was never updated — exactly one retry.

Budget: ``prepare_budget_titles_per_day`` counts prepare_run rows created
today (local day) in states running/ready/degraded/failed. ``unsupported``
rows are classification results, not builds, and do not spend budget.
``prepare_per_run_cap`` bounds how many builds one sync cycle starts.

The note model arrives through ``complete_factory`` (the watcher passes a
factory backed by the host LLM's ``watch_with_me_cues`` auxiliary task); the
Plex client arrives through ``client_factory``. Both are seams for tests.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import build
from . import onboarding
from . import prepare as prepare_mod
from . import plex
from . import store
from .config import Settings

log = logging.getLogger(__name__)

#: Bumped when the note pipeline changes invalidate previously built
#: timelines: a title built at an older version is rebuilt, not trusted.
BUILD_VERSION = 2

#: Re-read the Continue Watching hub at most this often while the watcher
#: runs, and immediately when the current session ends.
REFRESH_SECONDS = 600.0

#: Worker cadences when there is nothing to do / after an error.
_IDLE_WAIT_S = 5.0
_ERROR_WAIT_S = 60.0

#: Run states that represent a real build attempt (counted against the daily
#: budget). ``unsupported`` is a classification outcome, not a build.
BUDGET_STATES = ("running", "ready", "degraded", "failed")
UNSUPPORTED_CLASSES = ("none", "image_only")

_PREP_DETAIL = plex._PREP_STATE_DETAIL  # the §12 taxonomy copy, reused


# -- the day and the budget ----------------------------------------------------

def day_key(now: float | None = None) -> str:
    """The local-day key the daily budget accounts against."""
    return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))


def runs_started_on(conn, day: str) -> int:
    """Build attempts recorded for ``day`` (unsupported rows excluded)."""
    marks = ",".join("?" for _ in BUDGET_STATES)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM prepare_run WHERE day=? AND state IN ({marks})",
        (day, *BUDGET_STATES),
    ).fetchone()
    return int(row["n"])


def budget_remaining(conn, settings: Settings, *, day: str | None = None) -> int:
    return max(0, int(settings.prepare_budget_titles_per_day)) - runs_started_on(
        conn, day or day_key())


# -- prepare_run rows ----------------------------------------------------------

def record_run(conn, rating_key: str, *, state: str = "running",
               classification: str | None = None, remedy: str | None = None,
               build_version: int = BUILD_VERSION, now: float | None = None) -> int:
    """Insert a run row. ``running`` is written before the build starts so a
    crash leaves a recoverable row behind; every other state is terminal."""
    at = int(now if now is not None else time.time())
    cursor = conn.execute(
        "INSERT INTO prepare_run(rating_key,state,classification,started_at,"
        "build_version,day,remedy) VALUES(?,?,?,?,?,?,?)",
        (rating_key, state, classification, at, build_version, day_key(at), remedy),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(conn, run_id: int, *, state: str, duration_s: float | None = None,
               notes_kept: int | None = None, notes_total: int | None = None,
               error: str | None = None, classification: str | None = None,
               remedy: str | None = None, now: float | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE prepare_run SET state=?, finished_at=?, duration_s=?, notes_kept=?, "
            "notes_total=?, error=?, classification=COALESCE(?, classification), "
            "remedy=COALESCE(?, remedy) WHERE id=?",
            (state, int(now if now is not None else time.time()), duration_s,
             notes_kept, notes_total, (error or "")[:400] or None,
             classification, remedy, run_id),
        )


def latest_run(conn, rating_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM prepare_run WHERE rating_key=? ORDER BY id DESC LIMIT 1",
        (rating_key,),
    ).fetchone()
    return dict(row) if row else None


def recover_interrupted(conn, now: float | None = None) -> int:
    """Restart recovery: a ``running`` row means the process died mid-build.
    Mark it failed so the item is retried exactly once on the next pass —
    never duplicated, never silently lost."""
    with conn:
        cursor = conn.execute(
            "UPDATE prepare_run SET state='failed', error='interrupted: process restarted', "
            "finished_at=? WHERE state='running'",
            (int(now if now is not None else time.time()),),
        )
    return int(cursor.rowcount)


# -- the queue -----------------------------------------------------------------

def replace_queue(conn, items: list[dict], now: float | None = None) -> None:
    """The bounded work list for one sync cycle, in preparation order."""
    with conn:
        conn.execute("DELETE FROM prepare_queue")
        for position, item in enumerate(items):
            conn.execute(
                "INSERT INTO prepare_queue(rating_key,position,title,enqueued_at) "
                "VALUES(?,?,?,?)",
                (item["rating_key"], position, item.get("title"),
                 int(now if now is not None else time.time())),
            )


def queued_items(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM prepare_queue ORDER BY position").fetchall()
    return [dict(r) for r in rows]


def claim_next(conn) -> dict | None:
    """Pop the next item. Deleting before the build is what makes a crash
    retry-safe: the row is gone, so a restart re-derives the work from
    ``prepare_run`` + ``item.build_state`` instead of building twice."""
    row = conn.execute(
        "SELECT * FROM prepare_queue ORDER BY position LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    with conn:
        conn.execute("DELETE FROM prepare_queue WHERE rating_key=?", (row["rating_key"],))
    return dict(row)


def upsert_seen(conn, items: list[dict], now: float | None = None) -> None:
    """The dashboard's membership list: what the last hub snapshot held."""
    with conn:
        for item in items:
            conn.execute(
                "INSERT INTO prepare_seen(rating_key,title,grandparent_title,last_seen_at) "
                "VALUES(?,?,?,?) ON CONFLICT(rating_key) DO UPDATE SET "
                "title=excluded.title, grandparent_title=excluded.grandparent_title, "
                "last_seen_at=excluded.last_seen_at",
                (item["rating_key"], item.get("title"), item.get("grandparent_title"),
                 int(now if now is not None else time.time())),
            )


# -- decisions -----------------------------------------------------------------

def decide(conn, settings: Settings, item: dict, classification: str, *,
           build_version: int = BUILD_VERSION) -> str:
    """One queued item's disposition: ``prepared`` | ``building`` |
    ``unsupported`` | ``enqueue``.

    - ready at the current build version → ``prepared`` (nothing to do).
    - a build is running for it right now → ``building`` (a re-sync must not
      re-add an in-flight item: that would double-build it).
    - classified ``none``/``image_only`` with subtitle download off →
      ``unsupported`` (nothing to anchor notes to; the remedy names the fix).
    - otherwise → ``enqueue``.
    """
    rating_key = item["rating_key"]
    row = conn.execute(
        "SELECT build_state, build_version FROM item WHERE rating_key=?", (rating_key,)
    ).fetchone()
    if row and row["build_state"] == "ready" and int(row["build_version"] or 0) >= build_version:
        return "prepared"
    last = latest_run(conn, rating_key)
    if last and last["state"] == "running":
        return "building"
    if classification in UNSUPPORTED_CLASSES and not settings.download_subtitles:
        return "unsupported"
    return "enqueue"


def sync_queue(client, conn, settings: Settings, *, now: float | None = None,
               build_version: int = BUILD_VERSION) -> dict:
    """One preparation pass over the Continue Watching hub.

    Classification is the cheap per-item metadata read
    (RESEARCH-PREPARATION.md §14 Step 0, ~25 ms/item). Every seen item is
    recorded for the dashboard; supported items become the new bounded queue;
    unsupported items get one ``unsupported`` run row per classification
    (recorded with its remedy, not retried every pass).
    """
    if settings.prepare_depth == "off":
        return {"seen": 0, "queued": [], "prepared": [], "unsupported": [],
                "deferred": [], "stopped_reason": None}
    items = prepare_mod.collect_prewarm_queue(client, settings.prepare_depth)
    at = now if now is not None else time.time()
    upsert_seen(conn, items, now=at)
    prepared: list[str] = []
    queued: list[dict] = []
    unsupported: list[str] = []
    for item in items:
        rating_key = item.get("rating_key")
        if not rating_key:
            continue
        try:
            metadata = client.metadata(rating_key)
            classification = plex.classify_preparation(metadata)["state"]
        except plex.PlexError:
            classification = "unknown"
        decision = decide(conn, settings, item, classification, build_version=build_version)
        if decision == "prepared":
            prepared.append(rating_key)
        elif decision == "building":
            continue  # in flight; do not re-add
        elif decision == "unsupported":
            last = latest_run(conn, rating_key)
            already = (last and last["state"] == "unsupported"
                       and last["classification"] == classification)
            if not already:
                record_run(conn, rating_key, state="unsupported",
                           classification=classification, now=at,
                           remedy=_PREP_DETAIL[classification]["remedy"])
            unsupported.append(rating_key)
        else:
            queued.append(item)
    replace_queue(conn, queued, now=at)
    return {"seen": len(items), "queued": [i["rating_key"] for i in queued],
            "prepared": prepared, "unsupported": unsupported,
            "deferred": [], "stopped_reason": None}


# -- one build -----------------------------------------------------------------

def _wikipedia_sentences(metadata: dict) -> list[str]:
    """The title's Wikipedia facts (series article for episodes) — the
    scripts/build_title.py lookup, library-side so the worker can run it."""
    from .sources import wikipedia

    try:
        cache = wikipedia.ArticleCache()
        if metadata.get("type") == "episode":
            series = metadata.get("grandparentTitle") or metadata.get("title") or ""
            extract = (wikipedia.fetch_article(f"{series} (TV series)", cache=cache)
                       .get("extract") or "")
            if not extract:
                extract = (wikipedia.fetch_article(series, cache=cache)
                           .get("extract") or "")
        else:
            title = metadata.get("title") or ""
            year = metadata.get("year")
            query = f"{title} ({year} film)" if year else title
            extract = wikipedia.fetch_article(query, cache=cache).get("extract") or ""
        return wikipedia.section_facts(extract) if extract else []
    except Exception:  # noqa: BLE001 - an optional source
        return []


def _person_facts(metadata: dict, settings: Settings) -> list[dict]:
    from .sources import cast, wikipedia

    if not settings.entity_expansion:
        return []
    try:
        return cast.actor_facts(metadata, lambda name: wikipedia.fetch_article(name))
    except Exception:  # noqa: BLE001 - an optional source
        return []


def _expander(settings: Settings, complete, metadata: dict, *, wikipedia: bool):
    from . import entities

    if not settings.entity_expansion or not wikipedia:
        return None
    return entities.Expander(complete, cache=entities.EntityCache(),
                             excluded=entities.cast_names(metadata))


def _try_subtitle_download(client, conn, settings: Settings, rating_key: str) -> None:
    """``none``/``image_only`` with download_subtitles on: Plex's own
    search-and-attach may create the text sidecar the build can anchor to.
    subtitles.py is used, not modified; any failure just falls through."""
    from . import subtitles

    try:
        subtitles.download_subtitle(client, conn, rating_key, consent=True,
                                    languages=settings.subtitle_languages or ["en"])
    except Exception:  # noqa: BLE001 - the build reports no_transcript instead
        log.info("watch-with-me: subtitle download for %s did not succeed", rating_key)


def build_one(client, conn, settings: Settings, item: dict, complete, *,
              work_dir: Path, fetch_sentences: Callable = _wikipedia_sentences,
              fetch_person_facts: Callable = _person_facts,
              make_expander: Callable = _expander, now: float | None = None) -> dict:
    """Build one queued item end to end and record the run.

    Never raises: every outcome is a ``prepare_run`` row, so the dashboard
    and the next pass see exactly what happened. Returns a small summary;
    ``state`` is ``ready`` | ``degraded`` | ``failed`` | ``unsupported``.
    """
    rating_key = str(item.get("rating_key") or "")
    run_id = record_run(conn, rating_key, now=now)
    started = time.monotonic()
    try:
        metadata = client.metadata(rating_key)
        classification = plex.classify_preparation(metadata)["state"]
        conn.execute("UPDATE prepare_run SET classification=? WHERE id=?",
                     (classification, run_id))
        conn.commit()
        if classification in UNSUPPORTED_CLASSES and settings.download_subtitles:
            _try_subtitle_download(client, conn, settings, rating_key)
            metadata = client.metadata(rating_key)
            classification = plex.classify_preparation(metadata)["state"]

        srt, source = build.acquire_transcript(metadata, client, settings,
                                               work_dir, conn=conn)
        if not srt.strip():
            remedy = _PREP_DETAIL.get(classification, {}).get("remedy")
            state = "unsupported" if classification in UNSUPPORTED_CLASSES else "degraded"
            finish_run(conn, run_id, state=state, duration_s=time.monotonic() - started,
                       error="no_transcript", classification=classification, remedy=remedy)
            return {"rating_key": rating_key, "state": state, "notes_kept": 0,
                    "error": "no_transcript"}

        sentences = fetch_sentences(metadata)
        person_facts = fetch_person_facts(metadata, settings)
        expander = make_expander(settings, complete, metadata, wikipedia=bool(sentences))
        result = build.build_messages(metadata, srt, sentences, complete,
                                      person_facts=person_facts, expander=expander)
        result.transcript_src = source
        build.persist(conn, result, metadata, build_version=BUILD_VERSION)
        finish_run(conn, run_id, state=result.state,
                   duration_s=time.monotonic() - started,
                   notes_kept=len(result.messages),
                   notes_total=len(result.messages) + len(result.withheld),
                   error=result.error, classification=classification)
        return {"rating_key": rating_key, "state": result.state,
                "notes_kept": len(result.messages), "error": result.error}
    except Exception as exc:  # noqa: BLE001 - every outcome lands in the run row
        log.warning("watch-with-me: preparation build failed for %s: %s", rating_key, exc)
        finish_run(conn, run_id, state="failed", duration_s=time.monotonic() - started,
                   error=str(exc))
        return {"rating_key": rating_key, "state": "failed", "notes_kept": 0,
                "error": str(exc)}


# -- the worker ----------------------------------------------------------------

class PreparationWorker(threading.Thread):
    """One sequential preparation loop, driven by the Watcher.

    The watcher reports playing/idle via :meth:`set_playing`; the worker only
    builds while nothing plays (a build already running when playback starts
    finishes — interrupting a model call mid-flight would waste its cost —
    but no new build starts). The hub is re-read every ``REFRESH_SECONDS``
    while idle, and immediately after a session ends.
    """

    def __init__(self, settings: Settings, complete_factory=None, *,
                 client_factory=None, build=build_one) -> None:
        super().__init__(name="watch-with-me-prepare", daemon=True)
        self.settings = settings
        self._complete_factory = complete_factory
        self._client_factory = client_factory or self._default_client_factory
        self._build = build
        self._stop = threading.Event()
        # Starts set: assume playback until the watcher reports idle, so a
        # build can never start in the window before the first poll.
        self._playing = threading.Event()
        self._playing.set()
        self._wake = threading.Event()
        self._sync_due = 0.0
        self._builds_this_cycle = 0
        #: The last sync/cycle report; also what a deferred run reports.
        self.last_report: dict = {}

    # -- watcher-facing API -----------------------------------------------
    def set_playing(self, playing: bool, *, session_ended: bool = False) -> None:
        if playing:
            self._playing.set()
        else:
            self._playing.clear()
            if session_ended:
                self._sync_due = 0.0  # the queue may have changed: re-read now
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- internals ----------------------------------------------------------
    def _default_client_factory(self):
        creds = onboarding.load_credentials()
        if not creds:
            raise plex.PlexError("Plex is not linked")
        return plex.PlexClient(creds["base_url"], creds["token"],
                               creds.get("client_id", "watch-with-me"),
                               allow_loopback=self.settings.allow_loopback_plex)

    def _per_run_cap(self) -> int:
        return max(1, int(getattr(self.settings, "prepare_per_run_cap", 3) or 3))

    def run(self) -> None:  # noqa: D102 - the loop policy is in tick()
        conn = store.connect()
        try:
            recovered = recover_interrupted(conn)
            if recovered:
                log.info("watch-with-me: recovered %s interrupted preparation run(s)",
                         recovered)
            while not self._stop.is_set():
                outcome = self.tick(conn)
                self._wake.clear()
                if outcome == "built":
                    continue
                if outcome == "cap_reached":
                    self._wake.wait(max(1.0, self._sync_due - time.time()))
                elif outcome in ("deferred", "no_model", "error"):
                    self._wake.wait(_ERROR_WAIT_S)
                else:  # paused / waiting
                    self._wake.wait(_IDLE_WAIT_S)
        finally:
            conn.close()

    def tick(self, conn) -> str:
        """One turn of the loop. Returns what happened; also the test seam —
        tests drive ticks directly against a temp database, no threads."""
        if self._stop.is_set():
            return "stopped"
        if self._playing.is_set():
            return "paused"
        now = time.time()
        if now >= self._sync_due:
            try:
                self.last_report = sync_queue(self._client_factory(), conn,
                                              self.settings, now=now)
                self._builds_this_cycle = 0
            except Exception as exc:  # noqa: BLE001 - a dead Plex must not kill us
                log.warning("watch-with-me: preparation sync failed: %s", exc)
                self._sync_due = now + _ERROR_WAIT_S
                return "error"
            self._sync_due = now + REFRESH_SECONDS

        if not queued_items(conn):
            return "waiting"
        if self._builds_this_cycle >= self._per_run_cap():
            return "cap_reached"
        if budget_remaining(conn, self.settings) <= 0:
            deferred = [i["rating_key"] for i in queued_items(conn)]
            self.last_report = {**self.last_report, "deferred": deferred,
                                "stopped_reason": "daily_budget"}
            log.info("watch-with-me: preparation budget spent; deferred %s", deferred)
            return "deferred"

        complete = self._complete_factory() if self._complete_factory else None
        if complete is None:
            return "no_model"  # nothing resolved yet; back off and retry
        item = claim_next(conn)
        if item is None:
            return "waiting"
        try:
            with tempfile.TemporaryDirectory(prefix="wwm-prepare-") as tmp:
                outcome = self._build(self._client_factory(), conn, self.settings,
                                      item, complete, work_dir=Path(tmp))
        except Exception:  # noqa: BLE001 - a build bug must not kill the loop
            log.exception("watch-with-me: preparation build crashed for %s",
                          item.get("rating_key"))
            # The run row stays 'running'; recover_interrupted retries it once.
            return "error"
        self._builds_this_cycle += 1
        self.last_report = {**self.last_report, "last_build": outcome}
        return "built"


# -- dashboard -----------------------------------------------------------------

def dashboard_items(conn, settings: Settings, *, build_version: int = BUILD_VERSION,
                    now: float | None = None) -> list[dict]:
    """One row per item last seen in Continue Watching, newest snapshot first.

    States: ``prepared`` | ``queued`` | ``unsupported`` | ``failed``.
    Precedence: a fresh running row (building now) or a queue row → queued;
    ready at the current build version → prepared; otherwise the latest
    terminal run decides (unsupported names its remedy, failed its error).
    """
    at = now if now is not None else time.time()
    seen = conn.execute("SELECT * FROM prepare_seen ORDER BY last_seen_at DESC").fetchall()
    queued = {r["rating_key"] for r in conn.execute("SELECT rating_key FROM prepare_queue")}
    out: list[dict] = []
    for row in seen:
        key = row["rating_key"]
        item = conn.execute(
            "SELECT build_state, build_version, transcript_src FROM item WHERE rating_key=?",
            (key,),
        ).fetchone()
        last = latest_run(conn, key)
        running = last and last["state"] == "running" and at - last["started_at"] < 86400
        title = row["title"] or key
        if row["grandparent_title"] and row["grandparent_title"] != title:
            title = f"{row['grandparent_title']} — {title}"
        notes_kept = last["notes_kept"] if last else None
        updated_at = (last["finished_at"] or last["started_at"]) if last else None
        if running:
            state, detail = "queued", "building now"
        elif key in queued:
            state, detail = "queued", "waiting to build"
        elif item and item["build_state"] == "ready" and \
                int(item["build_version"] or 0) >= build_version:
            state = "prepared"
            detail = _prepared_detail(conn, key, item)
        elif last and last["state"] == "unsupported":
            state = "unsupported"
            detail = last["remedy"] or "nothing to anchor notes to"
        elif last and last["state"] == "failed":
            state = "failed"
            detail = (last["error"] or "unknown error")[:120]
        elif item and item["build_state"] == "ready":
            state, detail = "queued", "built with an older note pipeline; will rebuild"
        else:
            state, detail = "queued", "waiting to build"
        out.append({"rating_key": key, "title": title, "state": state,
                    "detail": detail, "notes_kept": notes_kept,
                    "updated_at": updated_at})
    return out


def _prepared_detail(conn, rating_key: str, item) -> str:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM message WHERE rating_key=?", (rating_key,)
    ).fetchone()
    count = int(row["n"])
    parts = [f"{count} notes"] if count else ["built"]
    if item["transcript_src"]:
        parts.append(f"transcript: {item['transcript_src']}")
    return ", ".join(parts)
