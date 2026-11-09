"""Opt-in subtitle download: Plex's own search-and-attach, with an immediate
text cache (RESEARCH-PREPARATION.md §4, §12 rows 8–11, §14 Step 3).

The contract, in order:

1. **Never mutate silently.** The only mutating call is ``PUT
   /library/metadata/{id}/subtitles`` and it happens only with explicit
   consent: either the user turned on ``download_subtitles`` (the automatic
   path) or they clicked the per-item action in the setup UI. ``consent``
   is a parameter of ``download_subtitle``; there is no other way to reach
   the PUT.
2. **The cached copy is the durable artifact.** Plex deletes
   search-downloaded subtitles when the user changes the selected track, so
   the sidecar is only a transport: attach, fetch immediately, store under
   ``store.data_dir()/subtitles/<rating_key>-<lang>.srt``, and serve every
   later read from the cache. Nothing here reads a subtitle from Plex
   twice.
3. **An empty library state is not an error.** No candidates, missing Plex
   Pass, a failed attach — each maps to a specific user-facing message
   (``FAILURE_MESSAGES``) and none of these functions raise for them.

Test rule: the Plex HTTP layer is always injected (``PlexClient(transport=)``
or a stub subclass); the cache root and DB connection are always explicit.
Nothing in this module touches the live library or the production data dir
when those are supplied.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from . import store
from .config import Settings
from .plex import PlexClient, PlexError, classify_preparation, text_streams
from .transcript import parse_srt

#: Ranking preference: these codecs carry text we can parse. ``ssa``/``subrip``
#: are also text but Plex's search results come back as srt/ass/vtt; the
#: general text list lives in plex.TEXT_SUB_CODECS.
DOWNLOAD_TEXT_CODECS = {"srt", "ass", "vtt"}

#: Delay between attachments in one prepare run, so a library pass cannot
#: hammer the provider (RESEARCH-PREPARATION.md §14 Step 3).
ATTACH_DELAY_S = 1.0

#: How long to wait for Plex to expose the sidecar after a successful PUT.
#: plexapi calls the attach "asynchronous… returns immediately before
#: subtitles are fully downloaded" (§4.2), so the metadata re-read polls.
SIDECAR_TRIES = 3
SIDECAR_POLL_S = 2.0

#: User-facing message per terminal state (RESEARCH-PREPARATION.md §12).
FAILURE_MESSAGES = {
    "none": "Plex found no subtitles for this title in your languages.",
    "search_unavailable": ("Subtitle search isn't available on this server "
                           "(needs Plex Pass and a signed-in owner)."),
    "attach_failed": "Couldn't download a subtitle for this title. Will retry.",
    "sidecar_missing": "Downloaded, but Plex isn't serving it yet — retrying later.",
    "unparsable": "A subtitle was found but couldn't be read.",
    "refused": ("Subtitle download is off. Enable download_subtitles to let the "
                "plugin write subtitles into your Plex library."),
}

#: ISO-639-1 (search query) → ISO-639-2/B (Plex result) for the languages the
#: setting is realistically going to hold. Unknown codes still match when
#: Plex echoes the same code back.
_LANG_2TO3 = {
    "en": "eng", "eng": "eng",
    "es": "spa", "spa": "spa",
    "fr": "fre", "fre": "fre", "fra": "fra",
    "de": "ger", "ger": "ger", "deu": "deu",
    "it": "ita", "ita": "ita",
    "pt": "por", "por": "por",
    "nl": "dut", "dut": "dut",
    "ja": "jpn", "jpn": "jpn",
    "ko": "kor", "kor": "kor",
    "zh": "zho", "zho": "zho", "chi": "zho",
    "sv": "swe", "swe": "swe",
    "no": "nor", "nor": "nor",
    "da": "dan", "dan": "dan",
    "fi": "fin", "fin": "fin",
    "pl": "pol", "pol": "pol",
    "ru": "rus", "rus": "rus",
}


def language_matches(preference: str, language_code: str | None) -> bool:
    """Does a result ``languageCode`` satisfy a preference-list entry?

    Preferences are ISO-639-1 (``en``), Plex results ISO-639-2/B (``eng``);
    either form matches itself and its mapped form (§4.1).
    """
    pref = (preference or "").strip().lower()
    code = (language_code or "").strip().lower()
    if not pref or not code:
        return False
    return pref == code or _LANG_2TO3.get(pref, pref) == _LANG_2TO3.get(code, code)


def rank_candidates(candidates: list[dict], language: str) -> list[dict]:
    """Best first: exact language match, then non-forced, then provider score,
    then a text codec over anything else (§4.1: ``score`` semantics are
    undocumented, so it is only a tiebreaker)."""
    def key(candidate: dict) -> tuple:
        return (
            not language_matches(language, candidate.get("languageCode")),
            bool(candidate.get("forced")),
            -int(candidate.get("score") or 0),
            (candidate.get("codec") or "").lower() not in DOWNLOAD_TEXT_CODECS,
        )
    return sorted(candidates, key=key)


def _result(state: str, rating_key: str, **extra) -> dict:
    message = FAILURE_MESSAGES.get(state)
    result = {"state": state, "rating_key": str(rating_key), "message": message}
    result.update(extra)
    return result


# -- cache ---------------------------------------------------------------------

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def cache_root(root: Path | None = None) -> Path:
    return (root or store.data_dir()) / "subtitles"


def cache_path(rating_key: str, language: str, root: Path | None = None) -> Path:
    safe_key = _SAFE_NAME.sub("_", str(rating_key))
    safe_lang = _SAFE_NAME.sub("_", str(language))
    return cache_root(root) / f"{safe_key}-{safe_lang}.srt"


def read_cache(conn: sqlite3.Connection, rating_key: str, language: str,
               root: Path | None = None) -> dict | None:
    """The cached subtitle, or None when absent, corrupt, or empty.

    A row whose file vanished or no longer parses is treated as absent and
    cleaned up, so the next caller refetches instead of trusting a corpse.
    """
    row = conn.execute(
        "SELECT * FROM subtitle_cache WHERE rating_key=? AND language=?",
        (str(rating_key), str(language)),
    ).fetchone()
    if row is None:
        return None
    path = Path(row["path"])
    try:
        text = path.read_text("utf-8", "replace")
    except OSError:
        text = ""
    if text.strip() and parse_srt(text):
        return {"text": text, "language": row["language"], "cues": int(row["cues"]),
                "path": str(path)}
    with conn:
        conn.execute("DELETE FROM subtitle_cache WHERE rating_key=? AND language=?",
                     (str(rating_key), str(language)))
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def write_cache(conn: sqlite3.Connection, rating_key: str, language: str, text: str,
                root: Path | None = None) -> int:
    """Store the text and index it. Returns the cue count (0 = unparsable;
    the caller decides whether that is a failure). Never stores the token —
    the file is subtitle text and the index is metadata about it."""
    path = cache_path(rating_key, language, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    cues = len(parse_srt(text))
    with conn:
        conn.execute(
            "INSERT INTO subtitle_cache(rating_key, language, path, cues, fetched_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(rating_key, language) DO UPDATE SET "
            "path=excluded.path, cues=excluded.cues, fetched_at=excluded.fetched_at",
            (str(rating_key), str(language), str(path), cues, int(time.time())),
        )
    return cues


def cached_transcript(conn: sqlite3.Connection, rating_key: str,
                      languages: list[str], root: Path | None = None) -> tuple | None:
    """(text, language, cues) for the first preference language with a valid
    cache entry — the "do I already have this" check that never touches Plex."""
    for language in languages:
        hit = read_cache(conn, rating_key, language, root)
        if hit is not None:
            return hit["text"], hit["language"], hit["cues"]
    return None


# -- sidecar discovery -----------------------------------------------------------

def _stream_ids(metadata: dict) -> set:
    return {
        stream.get("id")
        for media in metadata.get("Media") or []
        for part in media.get("Part") or []
        for stream in part.get("Stream") or []
    }


def find_sidecar_stream(metadata: dict, languages: list[str],
                        exclude_ids: set | None = None) -> dict | None:
    """The fetchable text subtitle stream for one of ``languages``.

    Prefers a stream Plex did not report before the attach (``exclude_ids``,
    the pre-attach stream ids) so a freshly downloaded sidecar wins over any
    pre-existing one; embedded streams (no ``key``) are never returned.
    """
    keyed = [s for s in text_streams(metadata) if s.get("key")]
    matching = [s for s in keyed
                if any(language_matches(lang, s.get("languageCode")) for lang in languages)]
    pool = matching or keyed
    if exclude_ids:
        fresh = [s for s in pool if s.get("id") not in exclude_ids]
        if fresh:
            pool = fresh
    return pool[0] if pool else None


# -- the download flow -------------------------------------------------------------

def download_subtitle(client: PlexClient, conn: sqlite3.Connection, rating_key: str, *,
                      consent: bool, languages: list[str],
                      hearing_impaired: int = 0, forced: int = 0,
                      root: Path | None = None, sleep=time.sleep,
                      sidecar_tries: int = SIDECAR_TRIES,
                      poll_interval: float = SIDECAR_POLL_S) -> dict:
    """Search → rank → attach → fetch → cache, for one item.

    ``consent`` is the ONLY gate on the mutating PUT: the automatic path
    passes ``settings.download_subtitles``; the per-item UI action passes
    True because the click is itself the consent. Without it the function
    refuses before touching the network for search.

    Never raises for the §12 failure states; each returns a result dict
    whose ``message`` is the user-facing text.
    """
    rating_key = str(rating_key)
    languages = [lang for lang in (languages or ["en"]) if lang] or ["en"]

    if not consent:
        return _result("refused", rating_key)

    cached = cached_transcript(conn, rating_key, languages, root)
    if cached is not None:
        return _result("cached", rating_key, language=cached[1], cues=cached[2])

    try:
        metadata = client.metadata(rating_key)
    except PlexError as exc:
        return _result("error", rating_key, message=f"Plex error: {exc}")
    if not metadata:
        return _result("error", rating_key,
                       message="Plex returned no metadata for this item.")

    preparation = classify_preparation(metadata)
    if preparation["state"] == "sidecar":
        # A fetchable subtitle already exists — never re-attach; just adopt
        # it. Only a language-matching stream counts: a sidecar in another
        # language is not a substitute for the requested one, and attaching
        # adds a new track rather than replacing the existing one.
        keyed = [s for s in text_streams(metadata)
                 if s.get("key")
                 and any(language_matches(lang, s.get("languageCode")) for lang in languages)]
        if keyed:
            stream = keyed[0]
            language = _stream_language(stream, languages)
            try:
                text = client.sidecar(stream["key"], stream.get("codec") or "srt")
            except PlexError:
                text = ""
            cues = len(parse_srt(text)) if text.strip() else 0
            if cues:
                write_cache(conn, rating_key, language, text, root)
                return _result("sidecar", rating_key, language=language, cues=cues)
            # Row 12 of the taxonomy: the sidecar exists but Plex will not
            # serve it. Report and retry later — attaching over an existing
            # sidecar is never the answer.
            return _result("sidecar_missing", rating_key, language=language)

    before_ids = _stream_ids(metadata)
    candidate = None
    candidate_language = None
    for language in languages:
        try:
            found = client.search_subtitles(rating_key, language,
                                            forced=forced, hearing_impaired=hearing_impaired)
        except PlexError:
            # Row 9 of the taxonomy: any non-200 (401/403 = no Plex Pass, 400 =
            # a bug in our query, network) maps to the same message, surfaced
            # once, never retried in a loop.
            return _result("search_unavailable", rating_key)
        ranked = rank_candidates(found, language)
        if ranked:
            candidate = ranked[0]
            candidate_language = language
            break
    if candidate is None:
        # Row 8: "no subtitles exist in any language we asked about" is a
        # library truth, not an error.
        return _result("none", rating_key)

    try:
        client.attach_subtitle(rating_key, candidate["key"],
                               (candidate.get("codec") or "srt").lower())
    except PlexError:
        return _result("attach_failed", rating_key, language=candidate_language)

    # The PUT is asynchronous on the Plex side (§4.2): poll the metadata until
    # the new stream with a key shows up, bounded.
    for attempt in range(sidecar_tries):
        if attempt:
            sleep(poll_interval)
        try:
            after = client.metadata(rating_key)
        except PlexError:
            after = {}
        stream = find_sidecar_stream(after, [candidate_language], exclude_ids=before_ids)
        if stream is not None:
            break
    else:
        stream = None
    if stream is None:
        return _result("sidecar_missing", rating_key, language=candidate_language)

    try:
        text = client.sidecar(stream["key"], stream.get("codec") or "srt")
    except PlexError:
        return _result("sidecar_missing", rating_key, language=candidate_language)
    if not text.strip():
        return _result("sidecar_missing", rating_key, language=candidate_language)

    language = _stream_language(stream, [candidate_language])
    cues = len(parse_srt(text))
    if not cues:
        # Row 14: got bytes, but not a usable transcript. Do not cache junk.
        return _result("unparsable", rating_key, language=language)
    write_cache(conn, rating_key, language, text, root)
    return _result("downloaded", rating_key, language=language, cues=cues)


def _stream_language(stream: dict, languages: list[str]) -> str:
    code = stream.get("languageCode") or ""
    for language in languages:
        if language_matches(language, code):
            return language
    return languages[0] if languages else "en"


# -- the automatic path ------------------------------------------------------------

def prepare_download_run(client: PlexClient, conn: sqlite3.Connection, items: list,
                         settings: Settings, *, root: Path | None = None,
                         sleep=time.sleep) -> dict:
    """The budgeted automatic pass over a preparation list.

    Runs only when ``settings.download_subtitles`` is on — that setting is
    the consent for the whole run. Each attachment is followed by
    ``ATTACH_DELAY_S`` of delay so a library pass cannot hammer the
    provider. Stops early when search is unavailable (there is no point
    continuing without Plex Pass) but keeps going across per-item failures.

    ``items`` are rating keys or dicts carrying one. Items that already have
    a cached copy or a sidecar are skipped; items the budget never reached
    are reported as deferred.
    """
    if not settings.download_subtitles:
        return {"ran": False, "reason": "disabled", "attached": [],
                "deferred": [], "skipped": [], "stopped_reason": None}

    budget = max(0, int(settings.subtitle_download_budget))
    languages = settings.subtitle_languages or ["en"]
    attached: list[dict] = []
    deferred: list[str] = []
    skipped: list[str] = []
    stopped_reason: str | None = None
    attachments_done = 0

    for item in items:
        rating_key = str(item.get("rating_key") if isinstance(item, dict) else item)
        if not rating_key:
            continue
        if cached_transcript(conn, rating_key, languages, root) is not None:
            skipped.append(rating_key)
            continue
        if budget <= 0:
            deferred.append(rating_key)
            continue

        try:
            metadata = client.metadata(rating_key)
        except PlexError:
            metadata = {}
        if metadata and classify_preparation(metadata)["state"] == "sidecar":
            skipped.append(rating_key)
            continue

        # The delay sits between attachments, not after the last one: a run
        # that just finished its final attach must not linger.
        if attachments_done:
            sleep(ATTACH_DELAY_S)
        result = download_subtitle(client, conn, rating_key, consent=True,
                                   languages=languages, root=root, sleep=sleep)
        if result["state"] in ("downloaded", "attach_failed", "sidecar_missing",
                               "unparsable"):
            budget -= 1
            attachments_done += 1
        if result["state"] == "downloaded":
            attached.append(result)
        elif result["state"] == "search_unavailable":
            stopped_reason = result["message"]
            break

    return {"ran": True, "reason": None, "attached": attached, "deferred": deferred,
            "skipped": skipped, "stopped_reason": stopped_reason}


def load_settings(conn: sqlite3.Connection) -> Settings:
    """The effective settings as last persisted by the plugin host. The
    dashboard runs in a different process from ``register()``, so the
    settings it needs arrive through runtime_state, not memory."""
    raw = store.get_state(conn, "settings_json", "{}") or "{}"
    try:
        return Settings.from_dict(json.loads(raw))
    except ValueError:
        return Settings()
