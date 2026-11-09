"""Subtitle download: Plex search-and-attach with immediate text cache.

Every test uses a fake Plex HTTP layer (``FakeTransport`` injected into the
real ``PlexClient``, so the URL shapes the client builds are exercised) plus
an explicit temp DB and cache root. Nothing touches a live library or the
production data dir (RESEARCH-PREPARATION.md §4, §12).
"""

from __future__ import annotations

import urllib.error
import urllib.parse

import pytest

from watch_with_me import build, store, subtitles
from watch_with_me.config import DOWNLOAD_SUBTITLES_HELP, Settings
from watch_with_me.plex import PlexClient

TOKEN = "token-should-never-be-stored"

SRT = (
    "1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"
    "2\n00:00:05,000 --> 00:00:07,000\nGeneral Kenobi.\n"
)

#: Captured search shape (RESEARCH-PREPARATION.md §4.1).
SEARCH_FIXTURE = {
    "MediaContainer": {
        "size": 2,
        "identifier": "com.plexapp.plugins.library",
        "Stream": [
            {"id": 239716, "codec": "srt", "languageCode": "eng",
             "key": "/library/streams/239716", "score": 18, "title": "English",
             "providerTitle": "OpenSubtitles"},
            {"id": 239720, "codec": "srt", "languageCode": "eng",
             "key": "/library/streams/239720", "score": 7, "hearingImpaired": True,
             "title": "English SDH", "providerTitle": "OpenSubtitles"},
        ],
    }
}


def stream(stream_id, codec="srt", lang="eng", key=None, **extra):
    s = {"id": stream_id, "streamType": 3, "codec": codec, "languageCode": lang}
    if key is not None:
        s["key"] = key
    s.update(extra)
    return s


def item_metadata(rating_key, streams):
    return {"ratingKey": str(rating_key), "title": "A Title", "type": "movie",
            "Media": [{"Part": [{"Stream": streams}]}]}


class FakeTransport:
    """Scripted Plex server. Records every request; serves the routes the
    subtitle flow uses; mutates item streams after an attach the way Plex
    does once the provider download lands."""

    def __init__(self):
        self.calls = []  # (method, path, raw_query)
        self.items = {}  # rk -> {"streams": [...], "search": {lang: [...]}}
        self.attached = []  # (rk, raw_query) for every PUT
        self.sidecar_text = SRT
        self.search_error = None  # HTTPError raised on GET .../subtitles
        self.attach_error = None  # HTTPError raised on PUT .../subtitles
        self.dead_sidecars = set()  # stream paths that 404
        self.expose_attached = True  # False = PUT accepted, download never lands

    def add_item(self, rating_key, streams=(), search=None):
        self.items[str(rating_key)] = {"streams": list(streams),
                                       "search": search or {},
                                       "attached_streams": []}

    def metadata_payload(self, rk):
        item = self.items[rk]
        streams = list(item["streams"])
        if self.expose_attached:
            streams += item["attached_streams"]
        return {"MediaContainer": {"Metadata": [
            item_metadata(rk, streams)]}}

    def __call__(self, method, url, headers, timeout, *, raw=False):
        parsed = urllib.parse.urlparse(url)
        path, query = parsed.path, parsed.query
        self.calls.append((method, path, query))
        params = urllib.parse.parse_qs(query)

        if method == "PUT" and path.startswith("/library/metadata/") \
                and path.endswith("/subtitles"):
            rk = path.split("/")[3]
            self.attached.append((rk, query))
            if self.attach_error is not None:
                raise self.attach_error
            key = params["key"][0]
            codec = params.get("codec", ["srt"])[0]
            stream_id = int(key.rstrip("/").rsplit("/", 1)[-1])
            item = self.items[rk]
            if not any(s.get("key") == key for s in item["attached_streams"]):
                item["attached_streams"].append(
                    stream(stream_id, codec=codec, lang="eng", key=key))
            return {}

        if method == "GET" and path.startswith("/library/metadata/") \
                and path.endswith("/subtitles"):
            if self.search_error is not None:
                raise self.search_error
            rk = path.split("/")[3]
            lang = params["language"][0]
            found = list(self.items[rk]["search"].get(lang, []))
            return {"MediaContainer": {"size": len(found), "Stream": found}}

        if method == "GET" and path.startswith("/library/metadata/"):
            rk = path.split("/")[3]
            return self.metadata_payload(rk)

        if method == "GET" and path.startswith("/library/streams/"):
            if path in self.dead_sidecars:
                raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
            return self.sidecar_text.encode("utf-8")

        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Real PlexClient on an injected transport, temp DB, temp cache root."""
    monkeypatch.setattr("watch_with_me.plex.validate_base_url",
                        lambda url, allow_loopback=False: url.rstrip("/"))
    transport = FakeTransport()
    client = PlexClient("http://plex.test:32400", TOKEN, "cid-test",
                        transport=transport)
    conn = store.connect(tmp_path / "data.db")
    root = tmp_path / "cache"
    yield client, conn, root, transport
    conn.close()


def no_sleep(_seconds):
    return None


def put_queries(transport):
    return [q for m, _p, q in transport.calls if m == "PUT"]


# -- search parsing -------------------------------------------------------------


def test_search_parsing_returns_candidates_from_fixture(env):
    client, _conn, _root, transport = env
    transport.add_item("65481", search={"en": SEARCH_FIXTURE["MediaContainer"]["Stream"]})
    found = client.search_subtitles("65481", "en")
    assert [f["id"] for f in found] == [239716, 239720]
    assert found[0]["codec"] == "srt" and found[0]["languageCode"] == "eng"
    assert found[0]["key"] == "/library/streams/239716"
    _method, path, query = transport.calls[-1]
    assert path == "/library/metadata/65481/subtitles"
    assert "language=en" in query and "forced=0" in query
    assert "hearingImpaired=0" in query


def test_search_empty_result_is_a_none_state_not_an_error(env):
    client, conn, root, transport = env
    transport.add_item("64885", search={})
    result = subtitles.download_subtitle(client, conn, "64885", consent=True,
                                         languages=["en"], root=root, sleep=no_sleep)
    assert result["state"] == "none"
    assert result["message"] == subtitles.FAILURE_MESSAGES["none"]


# -- ranking --------------------------------------------------------------------


def test_rank_language_beats_score():
    foreign = stream(1, lang="spa", key="/library/streams/1", score=9999)
    native = stream(2, lang="eng", key="/library/streams/2", score=2)
    ranked = subtitles.rank_candidates([foreign, native], "en")
    assert ranked[0]["id"] == 2


def test_rank_non_forced_beats_forced_despite_score():
    forced = stream(1, key="/library/streams/1", score=99, forced=True)
    plain = stream(2, key="/library/streams/2", score=1)
    ranked = subtitles.rank_candidates([forced, plain], "en")
    assert ranked[0]["id"] == 2


def test_rank_text_codec_beats_bitmap_on_tied_score():
    # ``score`` outranks codec in the ordering (it is checked first), so the
    # codec preference only breaks ties — same score, text wins.
    bitmap = stream(1, codec="pgs", key="/library/streams/1", score=50)
    text = stream(2, codec="srt", key="/library/streams/2", score=50)
    ranked = subtitles.rank_candidates([bitmap, text], "en")
    assert ranked[0]["id"] == 2


# -- attach ---------------------------------------------------------------------


def _attachable_item(transport, rk="65514", candidate=None):
    candidate = candidate or {"id": 99745, "codec": "srt", "languageCode": "eng",
                              "key": "/library/streams/99745", "score": 18}
    transport.add_item(rk, streams=[stream(100)], search={"en": [candidate]})


def test_attach_sends_exactly_one_put_with_urlencoded_key_and_codec(env):
    client, conn, root, transport = env
    _attachable_item(transport)
    result = subtitles.download_subtitle(client, conn, "65514", consent=True,
                                         languages=["en"], root=root, sleep=no_sleep)
    assert result["state"] == "downloaded"
    assert result["cues"] == 2
    assert len(transport.attached) == 1
    rk, query = transport.attached[0]
    assert rk == "65514"
    assert "key=%2Flibrary%2Fstreams%2F99745" in query
    assert "codec=srt" in query
    assert len(put_queries(transport)) == 1


def test_refused_without_consent_makes_no_requests(env):
    client, conn, root, transport = env
    _attachable_item(transport)
    result = subtitles.download_subtitle(client, conn, "65514", consent=False,
                                         languages=["en"], root=root, sleep=no_sleep)
    assert result["state"] == "refused"
    assert result["message"] == subtitles.FAILURE_MESSAGES["refused"]
    assert transport.calls == []


def test_existing_sidecar_is_adopted_never_reattached(env):
    client, conn, root, transport = env
    transport.add_item("65481", streams=[stream(200, key="/library/streams/200")],
                       search={"en": SEARCH_FIXTURE["MediaContainer"]["Stream"]})
    result = subtitles.download_subtitle(client, conn, "65481", consent=True,
                                         languages=["en"], root=root, sleep=no_sleep)
    assert result["state"] == "sidecar"
    assert result["cues"] == 2
    assert transport.attached == []
    # the adopted text is cached under the plugin's own root
    assert subtitles.read_cache(conn, "65481", "en", root) is not None


def test_unfetchable_existing_sidecar_never_reattaches(env):
    client, conn, root, transport = env
    transport.add_item("65481", streams=[stream(200, key="/library/streams/200")],
                       search={"en": SEARCH_FIXTURE["MediaContainer"]["Stream"]})
    transport.dead_sidecars.add("/library/streams/200.srt")
    result = subtitles.download_subtitle(client, conn, "65481", consent=True,
                                         languages=["en"], root=root, sleep=no_sleep)
    assert result["state"] == "sidecar_missing"
    assert transport.attached == []


# -- sidecar discovery ------------------------------------------------------------


def test_sidecar_discovery_prefers_newly_added_stream_not_embedded(env):
    transport = FakeTransport()
    _attachable_item(transport)
    before = transport.metadata_payload("65514")["MediaContainer"]["Metadata"][0]
    before_ids = subtitles._stream_ids(before)
    assert subtitles.find_sidecar_stream(before, ["en"]) is None  # embedded: no key
    transport("PUT", "http://plex.test:32400"
              "/library/metadata/65514/subtitles?key=%2Flibrary%2Fstreams%2F99745"
              "&codec=srt", {}, 10)
    after = transport.metadata_payload("65514")["MediaContainer"]["Metadata"][0]
    found = subtitles.find_sidecar_stream(after, ["en"], exclude_ids=before_ids)
    assert found is not None and found["id"] == 99745
    assert found.get("key") == "/library/streams/99745"


def test_post_attach_poll_waits_for_the_new_stream(env):
    """The PUT is asynchronous server-side; a metadata re-read that has not
    caught up yet must poll, not fail."""
    client, conn, root, transport = env
    _attachable_item(transport)
    real_metadata = transport.metadata_payload

    def lagging_metadata(rk):
        # First post-attach read still shows the pre-attach streams.
        payload = real_metadata(rk)
        md = payload["MediaContainer"]["Metadata"][0]
        if any(m == "PUT" for m, _p, _q in transport.calls):
            streams_now = [s for m in md["Media"] for p in m["Part"] for s in p["Stream"]]
            if len(streams_now) > 1:
                return payload
            md["Media"][0]["Part"][0]["Stream"] = [stream(100)]
        return payload

    transport.metadata_payload = lagging_metadata  # type: ignore[method-assign]
    result = subtitles.download_subtitle(client, conn, "65514", consent=True,
                                         languages=["en"], root=root, sleep=no_sleep,
                                         poll_interval=0)
    assert result["state"] == "downloaded"


# -- cache ------------------------------------------------------------------------


def test_second_call_serves_from_cache_without_plex(env):
    client, conn, root, transport = env
    _attachable_item(transport)
    first = subtitles.download_subtitle(client, conn, "65514", consent=True,
                                        languages=["en"], root=root, sleep=no_sleep)
    assert first["state"] == "downloaded"
    calls_after_first = len(transport.calls)
    second = subtitles.download_subtitle(client, conn, "65514", consent=True,
                                         languages=["en"], root=root, sleep=no_sleep)
    assert second["state"] == "cached"
    assert len(transport.calls) == calls_after_first  # no Plex traffic at all


def test_cache_row_records_cue_count_and_never_the_token(env):
    client, conn, root, transport = env
    _attachable_item(transport)
    subtitles.download_subtitle(client, conn, "65514", consent=True,
                                languages=["en"], root=root, sleep=no_sleep)
    row = conn.execute("SELECT * FROM subtitle_cache WHERE rating_key='65514'") \
        .fetchone()
    assert row["language"] == "en" and row["cues"] == 2
    path = root / "subtitles" / "65514-en.srt"
    assert path.read_text("utf-8") == SRT
    assert TOKEN not in path.read_text("utf-8")
    assert TOKEN not in str(dict(row))


@pytest.mark.parametrize("corrupt", ["not an srt at all", ""])
def test_corrupt_or_empty_cache_is_absent_and_refetched(env, corrupt):
    client, conn, root, transport = env
    _attachable_item(transport)
    subtitles.download_subtitle(client, conn, "65514", consent=True,
                                languages=["en"], root=root, sleep=no_sleep)
    path = root / "subtitles" / "65514-en.srt"
    path.write_text(corrupt, encoding="utf-8")
    assert subtitles.read_cache(conn, "65514", "en", root) is None
    # the corpse row was cleaned up too
    assert conn.execute("SELECT COUNT(*) c FROM subtitle_cache").fetchone()["c"] == 0
    again = subtitles.download_subtitle(client, conn, "65514", consent=True,
                                        languages=["en"], root=root, sleep=no_sleep)
    assert again["state"] in ("downloaded", "sidecar")  # refetched, re-cached
    assert subtitles.read_cache(conn, "65514", "en", root)["text"] == SRT


def test_plex_deleted_sidecar_still_serves_from_cache(env):
    client, conn, root, transport = env
    _attachable_item(transport)
    subtitles.download_subtitle(client, conn, "65514", consent=True,
                                languages=["en"], root=root, sleep=no_sleep)
    transport.dead_sidecars.add("/library/streams/99745.srt")
    calls_before = len(transport.calls)
    text, language, cues = subtitles.cached_transcript(conn, "65514", ["en"], root)
    assert text == SRT and language == "en" and cues == 2
    # the build's acquisition path answers from the cache and never asks Plex
    metadata_now = item_metadata("65514", [stream(100)])  # sidecar gone from Plex
    acquired, source = build.acquire_transcript(metadata_now, client, Settings(),
                                                root, conn=conn)
    assert source == "cache" and acquired == SRT
    stream_calls = [c for c in transport.calls[calls_before:]
                    if c[1].startswith("/library/streams/")]
    assert stream_calls == []


def test_unparsable_sidecar_is_not_cached(env):
    client, conn, root, transport = env
    _attachable_item(transport)
    transport.sidecar_text = "definitely not a subtitle"
    result = subtitles.download_subtitle(client, conn, "65514", consent=True,
                                         languages=["en"], root=root, sleep=no_sleep)
    assert result["state"] == "unparsable"
    assert conn.execute("SELECT COUNT(*) c FROM subtitle_cache").fetchone()["c"] == 0


# -- budgeted prepare run -----------------------------------------------------------


def _settings(**overrides):
    base = {"download_subtitles": True, "subtitle_languages": ["en"],
            "subtitle_download_budget": 5}
    base.update(overrides)
    return Settings(**base)


def test_prepare_run_attaches_budget_and_defers_the_rest(env):
    client, conn, root, transport = env
    for rk in range(60001, 60009):  # 8 candidates
        _attachable_item(transport, rk=str(rk))
    report = subtitles.prepare_download_run(client, conn,
                                            [str(rk) for rk in range(60001, 60009)],
                                            _settings(), root=root, sleep=no_sleep)
    assert report["ran"] is True
    assert len(report["attached"]) == 5
    assert len(put_queries(transport)) == 5
    assert sorted(report["deferred"]) == [str(rk) for rk in range(60006, 60009)]


def test_prepare_run_disabled_by_default_makes_no_requests(env):
    client, conn, root, transport = env
    _attachable_item(transport, rk="60001")
    report = subtitles.prepare_download_run(client, conn, ["60001"],
                                            _settings(download_subtitles=False),
                                            root=root, sleep=no_sleep)
    assert report == {"ran": False, "reason": "disabled", "attached": [],
                      "deferred": [], "skipped": [], "stopped_reason": None}
    assert transport.calls == []


def test_prepare_run_skips_already_cached_items(env):
    client, conn, root, transport = env
    _attachable_item(transport, rk="60001")
    _attachable_item(transport, rk="60002")
    subtitles.download_subtitle(client, conn, "60001", consent=True,
                                languages=["en"], root=root, sleep=no_sleep)
    transport.calls.clear()
    report = subtitles.prepare_download_run(client, conn, ["60001", "60002"],
                                            _settings(subtitle_download_budget=1),
                                            root=root, sleep=no_sleep)
    assert report["skipped"] == ["60001"]
    assert len(report["attached"]) == 1
    assert put_queries(transport) != []  # only 60002 was attached


def test_prepare_run_stops_when_search_is_unavailable(env):
    client, conn, root, transport = env
    for rk in range(60001, 60004):
        _attachable_item(transport, rk=str(rk))
    transport.search_error = urllib.error.HTTPError(
        "http://plex.test", 401, "Unauthorized", hdrs=None, fp=None)
    report = subtitles.prepare_download_run(client, conn,
                                            [str(rk) for rk in range(60001, 60004)],
                                            _settings(), root=root, sleep=no_sleep)
    assert report["stopped_reason"] == subtitles.FAILURE_MESSAGES["search_unavailable"]
    assert transport.attached == []


def test_prepare_run_delays_between_attachments(env):
    client, conn, root, transport = env
    for rk in range(60001, 60004):
        _attachable_item(transport, rk=str(rk))
    delays = []
    subtitles.prepare_download_run(client, conn,
                                   [str(rk) for rk in range(60001, 60004)],
                                   _settings(), root=root, sleep=delays.append)
    # one delay between each pair of attachments, none trailing the last
    assert delays == [subtitles.ATTACH_DELAY_S, subtitles.ATTACH_DELAY_S]


# -- failure taxonomy: specific messages, none raise ----------------------------------


def test_failure_attach_http_error_maps_to_message(env):
    client, conn, root, transport = env
    _attachable_item(transport)
    transport.attach_error = urllib.error.HTTPError(
        "http://plex.test", 500, "Server Error", hdrs=None, fp=None)
    result = subtitles.download_subtitle(client, conn, "65514", consent=True,
                                         languages=["en"], root=root, sleep=no_sleep)
    assert result["state"] == "attach_failed"
    assert result["message"] == subtitles.FAILURE_MESSAGES["attach_failed"]


def test_failure_401_means_plex_pass_missing(env):
    client, conn, root, transport = env
    _attachable_item(transport)
    transport.search_error = urllib.error.HTTPError(
        "http://plex.test", 401, "Unauthorized", hdrs=None, fp=None)
    result = subtitles.download_subtitle(client, conn, "65514", consent=True,
                                         languages=["en"], root=root, sleep=no_sleep)
    assert result["state"] == "search_unavailable"
    assert "Plex Pass" in result["message"]
    assert transport.attached == []


def test_failure_sidecar_never_appears_after_successful_put(env):
    client, conn, root, transport = env
    candidate = {"id": 99745, "codec": "srt", "languageCode": "eng",
                 "key": "/library/streams/99745", "score": 18}
    # Plex accepts the PUT but the provider download never lands: the metadata
    # re-reads keep showing only the embedded stream.
    transport.add_item("65514", streams=[stream(100)], search={"en": [candidate]})
    transport.expose_attached = False
    result = subtitles.download_subtitle(client, conn, "65514", consent=True,
                                         languages=["en"], root=root, sleep=no_sleep,
                                         sidecar_tries=2, poll_interval=0)
    assert result["state"] == "sidecar_missing"
    assert result["message"] == subtitles.FAILURE_MESSAGES["sidecar_missing"]
    assert len(transport.attached) == 1  # exactly one attach attempt was made


def test_consent_help_text_states_the_four_facts():
    for fragment in ("WRITES subtitle files into your Plex library", "Plex Pass",
                     "remove", "cached copy"):
        assert fragment in DOWNLOAD_SUBTITLES_HELP
