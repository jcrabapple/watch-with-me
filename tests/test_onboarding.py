"""Onboarding unit tests: PIN flow, discovery, probing, storage, redaction.

All HTTP is driven through the ``transport`` seam — a callable
``(method, url, headers, timeout) -> dict`` that raises the same
``urllib.error`` exceptions the real stack raises.
"""

from __future__ import annotations

import json
import logging
import urllib.error

import pytest

from watch_with_me import onboarding, store
from watch_with_me.plex import PlexError

TOKEN = "tok-live-9f8e7d"


class FakeTransport:
    """Route requests by URL prefix to handler callables."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[dict] = []

    def __call__(self, method: str, url: str, headers: dict, timeout: float) -> dict:
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "timeout": timeout})
        # Longest-prefix match: "/pins" must not shadow "/pins/1".
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                handler = self.routes[prefix]
                if callable(handler):
                    return handler(method, url, headers)
                return handler  # a plain payload dict
        raise AssertionError(f"unexpected request {method} {url}")


def dead_uri(method, url, headers):
    raise urllib.error.URLError("timed out")


def unauthorized(method, url, headers):
    raise urllib.error.HTTPError(url, 401, "unauthorized", {}, None)


def ok_identity(method, url, headers):
    return {"MediaContainer": {"machineIdentifier": "mach-1", "version": "1.40.0"}}


# -- PIN creation / polling ------------------------------------------------------

def test_create_pin_posts_strong_false_and_parses_response():
    fake = FakeTransport({"https://plex.tv/api/v2/pins": {
        "id": 481516, "code": "2342", "expiresAt": 1893456000}})
    pin = onboarding.create_pin("cid-1", transport=fake)

    assert pin["id"] == "481516"
    assert pin["code"] == "2342"
    assert pin["expires_at"] == 1893456000
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert "strong=false" in call["url"]
    assert call["headers"]["X-Plex-Product"] == "watch-with-me"
    assert call["headers"]["X-Plex-Version"]
    assert call["headers"]["X-Plex-Client-Identifier"] == "cid-1"
    assert "X-Plex-Token" not in call["headers"]  # PIN creation is pre-auth


def test_poll_pin_returns_token_when_it_appears():
    seen = {"n": 0}

    def pin_state(method, url, headers):
        seen["n"] += 1
        return {} if seen["n"] < 2 else {"authToken": TOKEN}

    fake = FakeTransport({"https://plex.tv/api/v2/pins/481516": pin_state})
    token = onboarding.poll_pin_token("481516", "cid-1", transport=fake,
                                      sleep=lambda s: None, monotonic=iter([0, 2]).__next__)
    assert token == TOKEN
    assert fake.calls[0]["headers"]["X-Plex-Client-Identifier"] == "cid-1"


def test_poll_pin_hard_timeout_raises():
    fake = FakeTransport({"https://plex.tv/api/v2/pins/1": {}})
    clock = iter([0, 2, 4, 6]).__next__
    with pytest.raises(PlexError, match="Timed out"):
        onboarding.poll_pin_token("1", "cid-1", transport=fake, timeout_s=5,
                                  interval_s=2, sleep=lambda s: None, monotonic=clock)


# -- server discovery -------------------------------------------------------------

RESOURCES = [
    {"name": "NAS", "clientIdentifier": "mach-1", "owned": True,
     "provides": "server,player,sync",
     "connections": [
         {"uri": "http://172.17.0.2:32400", "local": True, "relay": False},
         {"uri": "http://192.168.1.20:32400/", "local": True, "relay": False},
     ]},
    {"name": "Friend's Server", "clientIdentifier": "mach-2", "owned": False,
     "provides": "server",
     "connections": [
         {"uri": "https://1-2-3-4.abcdef1234.plex.direct:32400",
          "local": False, "relay": False}]},
    {"name": "Pixel 8", "clientIdentifier": "mach-3", "owned": True,
     "provides": "player,sync", "connections": []},
]


def test_resources_filter_drops_non_servers_and_labels_access():
    servers = onboarding.filter_server_resources(RESOURCES)
    assert [s["name"] for s in servers] == ["NAS", "Friend's Server"]
    assert servers[0]["owned"] is True
    assert servers[0]["access"] == "your server"
    assert servers[1]["owned"] is False
    assert servers[1]["access"] == "shared with you"
    # trailing slashes are normalised so URI concatenation is safe
    assert servers[0]["connections"][1]["uri"] == "http://192.168.1.20:32400"


def test_fetch_resources_uses_token_header_and_query():
    fake = FakeTransport({"https://plex.tv/api/v2/resources": RESOURCES})
    payload = onboarding.fetch_resources(TOKEN, "cid-1", transport=fake)
    assert payload == RESOURCES
    call = fake.calls[0]
    assert "includeHttps=1" in call["url"] and "includeRelay=1" in call["url"]
    assert call["headers"]["X-Plex-Token"] == TOKEN


# -- connection probing -----------------------------------------------------------

def test_probe_skips_docker_bridge_and_picks_answering_uri():
    servers = onboarding.filter_server_resources(RESOURCES)
    fake = FakeTransport({
        "http://172.17.0.2:32400": dead_uri,        # docker bridge: local:true, dead
        "http://192.168.1.20:32400": ok_identity,
    })
    choice = onboarding.choose_server(servers[:1], TOKEN, "cid-1", transport=fake)
    assert choice["uri"] == "http://192.168.1.20:32400"
    assert choice["relay_fallback"] is False
    assert choice["answering"] == ["http://192.168.1.20:32400"]


def test_relay_connection_is_not_preferred():
    connections = [
        {"uri": "https://relay.plex.com:32400", "local": False, "relay": True},
        {"uri": "http://10.0.0.5:32400", "local": True, "relay": False},
    ]
    fake = FakeTransport({
        "https://relay.plex.com:32400": ok_identity,
        "http://10.0.0.5:32400": ok_identity,
    })
    choice = onboarding.choose_connection(connections, TOKEN, "cid-1", transport=fake)
    assert choice["uri"] == "http://10.0.0.5:32400"
    assert choice["relay"] is False
    assert choice["relay_fallback"] is False


def test_relay_is_explicit_fallback_when_nothing_else_answers():
    connections = [
        {"uri": "http://10.0.0.5:32400", "local": True, "relay": False},
        {"uri": "https://relay.plex.com:32400", "local": False, "relay": True},
    ]
    fake = FakeTransport({"https://relay.plex.com:32400": ok_identity,
                          "http://10.0.0.5:32400": dead_uri})
    choice = onboarding.choose_connection(connections, TOKEN, "cid-1", transport=fake)
    assert choice["uri"] == "https://relay.plex.com:32400"
    assert choice["relay"] is True
    assert choice["relay_fallback"] is True


def test_choose_server_prefers_owned_over_shared():
    servers = onboarding.filter_server_resources(RESOURCES)
    fake = FakeTransport({
        "http://172.17.0.2:32400": dead_uri,
        "http://192.168.1.20:32400": dead_uri,  # owned NAS unreachable
        "https://1-2-3-4.abcdef1234.plex.direct:32400": ok_identity,
    })
    choice = onboarding.choose_server(servers, TOKEN, "cid-1", transport=fake)
    assert choice["server"]["name"] == "Friend's Server"
    assert choice["server"]["access"] == "shared with you"


def test_no_answering_connection_raises():
    fake = FakeTransport({"http://172.17.0.2:32400": dead_uri,
                          "http://192.168.1.20:32400": dead_uri})
    servers = onboarding.filter_server_resources(RESOURCES)[:1]
    with pytest.raises(PlexError, match="No reachable Plex server"):
        onboarding.choose_server(servers, TOKEN, "cid-1", transport=fake)


def test_probe_reraises_401_so_the_flow_can_say_relink():
    fake = FakeTransport({"http://192.168.1.20:32400": unauthorized})
    with pytest.raises(PlexError) as excinfo:
        onboarding.probe_connection("http://192.168.1.20:32400", TOKEN, "cid-1",
                                    transport=fake)
    assert excinfo.value.status == 401


# -- storage ---------------------------------------------------------------------

def test_save_credentials_writes_shape_and_mode(tmp_path):
    path = tmp_path / "sub" / "plex-credentials.json"
    onboarding.save_credentials(base_url="http://192.168.1.20:32400", token=TOKEN,
                                client_id="cid-1", server_name="NAS",
                                machine_id="mach-1", path=path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    body = json.loads(path.read_text("utf-8"))
    assert body == {"base_url": "http://192.168.1.20:32400", "token": TOKEN,
                    "client_id": "cid-1", "server_name": "NAS",
                    "machine_id": "mach-1"}
    # round-trip through the loader
    assert onboarding.load_credentials(path=path) == body


def test_load_credentials_missing_or_corrupt(tmp_path, caplog):
    assert onboarding.load_credentials(path=tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert onboarding.load_credentials(path=bad) is None
    assert "unreadable credential file" in caplog.text


def test_client_identifier_is_generated_once_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    first = onboarding.client_identifier()
    assert first
    assert (tmp_path / "plex-client-id").stat().st_mode & 0o777 == 0o600
    assert onboarding.client_identifier() == first
    (tmp_path / "plex-client-id").unlink()
    assert onboarding.client_identifier() != first


# -- readiness sample --------------------------------------------------------------

def _playable_stream(codec="srt", key=None, stream_type=3):
    return {"streamType": stream_type, "codec": codec, "key": key}


def _metadata(title, streams):
    return {"title": title, "type": "movie", "year": 2026,
            "Media": [{"Part": [{"Stream": streams}]}]}


def _recent_item(rating_key, type_="movie", title=None, grandparent=None):
    item = {"ratingKey": str(rating_key), "type": type_, "title": title or f"Title {rating_key}",
            "year": 2026}
    if grandparent:
        item["grandparentTitle"] = grandparent
    return item


class StubClient:
    """The readiness seam: a recently_added listing plus per-item metadata.

    Mirrors the real client's split — ``/library/recentlyAdded`` lists rows
    (shows, seasons, movies, episodes) without ``Stream``; only
    ``/library/metadata/{ratingKey}`` carries the streams. ``fail_keys``
    simulates a metadata fetch error for those items.
    """

    def __init__(self, items, metadata_by_key=None, fail_keys=()):
        self._items = items
        self._metadata = metadata_by_key or {}
        self._fail_keys = set(fail_keys)
        self.fetched = []

    def recently_added(self, limit=10):
        return self._items[:limit]

    def metadata(self, rating_key):
        self.fetched.append(str(rating_key))
        if str(rating_key) in self._fail_keys:
            raise PlexError(f"Plex HTTP 500 for /library/metadata/{rating_key}", status=500)
        return self._metadata.get(str(rating_key), {})


def test_readiness_sample_reports_sidecar_coverage():
    items = [_recent_item(i) for i in range(10)]
    by_key = {str(i): _metadata(f"Title {i}",
                                [_playable_stream(key="/library/streams/1.srt")] if i < 3
                                else [_playable_stream(key=None)])
              for i in range(10)}
    sample = onboarding.readiness_sample(StubClient(items, by_key))
    assert sample["sampled"] == 10
    assert sample["with_sidecar"] == 3
    assert "7 of 10" in sample["note"]
    assert "download subtitles in Plex, or set an extract command" in sample["note"]


def test_readiness_sample_full_coverage_is_positive():
    items = [_recent_item(i) for i in range(4)]
    by_key = {str(i): _metadata(f"Title {i}", [_playable_stream(key="/x.srt")])
              for i in range(4)}
    sample = onboarding.readiness_sample(StubClient(items, by_key))
    assert sample["with_sidecar"] == 4
    assert sample["note"].startswith("All 4 sampled titles")


def test_readiness_sample_empty_library():
    sample = onboarding.readiness_sample(StubClient([]))
    assert sample["sampled"] == 0
    assert "No recent items" in sample["note"]


def test_readiness_sample_classifies_each_prep_state():
    sidecar = _metadata("Has Sidecar", [_playable_stream(key="/library/streams/9.srt")])
    embedded = _metadata("Embedded Only", [_playable_stream(key=None)])
    image = _metadata("Bitmap Only", [_playable_stream(codec="hdmv_pgs_subtitle")])
    none = _metadata("No Subs", [])
    items = [_recent_item(k, title=t) for k, t in
             (("a", "Has Sidecar"), ("b", "Embedded Only"),
              ("c", "Bitmap Only"), ("d", "No Subs"))]
    by_key = {"a": sidecar, "b": embedded, "c": image, "d": none}
    sample = onboarding.readiness_sample(StubClient(items, by_key))
    states = {c["title"]: c["state"] for c in sample["items"]}
    assert states == {"Has Sidecar": "sidecar", "Embedded Only": "embedded",
                      "Bitmap Only": "image_only", "No Subs": "none"}
    by_title = {c["title"]: c for c in sample["items"]}
    assert by_title["Has Sidecar"]["message"]
    assert by_title["Embedded Only"]["remedy"]
    assert "with_sidecar" in sample and sample["with_sidecar"] == 1


def test_readiness_sample_resolves_playable_items_and_fetches_metadata():
    # recently_added returns shows and seasons first, like the live endpoint.
    items = [{"ratingKey": "100", "type": "show", "title": "A Show"},
             {"ratingKey": "101", "type": "season", "title": "Season 2",
              "parentTitle": "A Show"},
             _recent_item("102", type_="episode", title="Pilot", grandparent="A Show"),
             _recent_item("103", type_="movie", title="A Film")]
    by_key = {
        "102": _metadata("Pilot", [_playable_stream(key="/library/streams/7.srt")]),
        "103": _metadata("A Film", [_playable_stream(key=None)]),
    }
    client = StubClient(items, by_key)
    sample = onboarding.readiness_sample(client, limit=10)
    # Only the playable items were sampled, and each was re-fetched by key.
    assert sample["sampled"] == 2
    assert client.fetched == ["102", "103"]
    assert sample["with_sidecar"] == 1


def test_readiness_sample_metadata_error_classifies_unknown_not_failure():
    items = [_recent_item("9")]
    client = StubClient(items, {}, fail_keys={"9"})
    sample = onboarding.readiness_sample(client)
    assert sample["sampled"] == 1
    assert sample["items"][0]["state"] == "unknown"
    assert sample["with_sidecar"] == 0


# -- error surfacing: 401 -> relink, token never leaks ------------------------------

def test_401_maps_to_relink_message():
    exc = PlexError("Plex HTTP 401 for /library/sections", status=401)
    assert onboarding.user_message(exc) == onboarding.RELINK_MESSAGE


def test_other_errors_keep_their_message():
    exc = PlexError("Plex unreachable: timed out")
    message = onboarding.user_message(exc)
    assert "Plex error" in message
    assert TOKEN not in message


def test_token_absent_from_every_log_line(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    fake = FakeTransport({
        "https://plex.tv/api/v2/pins": {"id": 1, "code": "2342", "expiresAt": 0},
        "https://plex.tv/api/v2/pins/1": {"authToken": TOKEN},
        "https://plex.tv/api/v2/resources": RESOURCES,
        "http://172.17.0.2:32400": dead_uri,
        "http://192.168.1.20:32400": ok_identity,
        "https://1-2-3-4.abcdef1234.plex.direct:32400": ok_identity,
    })
    with caplog.at_level(logging.DEBUG):
        onboarding.create_pin("cid-1", transport=fake)
        onboarding.poll_pin_token("1", "cid-1", transport=fake,
                                  sleep=lambda s: None, monotonic=iter([0, 2]).__next__)
        servers = onboarding.filter_server_resources(
            onboarding.fetch_resources(TOKEN, "cid-1", transport=fake))
        onboarding.choose_server(servers, TOKEN, "cid-1", transport=fake)
        path = tmp_path / "plex-credentials.json"
        onboarding.save_credentials(base_url="http://192.168.1.20:32400", token=TOKEN,
                                    client_id="cid-1", server_name="NAS",
                                    machine_id="mach-1", path=path)
        onboarding.load_credentials(path=path)
        onboarding.user_message(PlexError("Plex HTTP 401 for /library/sections", status=401))
    assert TOKEN not in caplog.text


# -- the /watch setup CLI text flow -------------------------------------------------

def _patch_cli_flow(monkeypatch, verify):
    monkeypatch.setattr(onboarding, "create_pin",
                        lambda cid: {"id": "1", "code": "WXYZ", "expires_at": 0})
    monkeypatch.setattr(onboarding, "poll_pin_token", lambda pid, cid, **kw: TOKEN)
    monkeypatch.setattr(onboarding, "fetch_resources", lambda tok, cid: RESOURCES)
    monkeypatch.setattr(onboarding, "probe_connection",
                        lambda uri, tok, cid, **kw: "192.168." in uri)
    monkeypatch.setattr(onboarding, "verify_server", verify)


def test_watch_setup_cli_prints_code_link_and_verification(tmp_path, monkeypatch):
    from watch_with_me import plugin

    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)

    def verify(uri, tok, cid, **kw):
        return {"library_count": 2, "library_names": ["Movies", "TV"],
                "readiness": {"sampled": 10, "with_sidecar": 8, "items": [],
                              "note": "2 of 10 sampled titles have no subtitle file — "
                                      "download subtitles in Plex, or set an extract command."}}

    _patch_cli_flow(monkeypatch, verify)
    text = plugin._setup()

    assert "https://plex.tv/link" in text
    assert "WXYZ" in text
    assert "NAS" in text
    assert "your server" in text
    assert "Libraries: 2 (Movies, TV)" in text
    assert "2 of 10" in text
    assert "Credentials saved" in text
    assert TOKEN not in text  # the CLI user never sees the token

    creds = json.loads((tmp_path / "plex-credentials.json").read_text("utf-8"))
    assert creds["token"] == TOKEN


def test_watch_setup_cli_401_returns_relink_message(tmp_path, monkeypatch):
    from watch_with_me import plugin
    from watch_with_me.plex import PlexError

    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)

    def verify_401(uri, tok, cid, **kw):
        raise PlexError("Plex HTTP 401 for /library/sections", status=401)

    _patch_cli_flow(monkeypatch, verify_401)
    text = plugin._setup()
    assert text == onboarding.RELINK_MESSAGE
    assert not (tmp_path / "plex-credentials.json").exists()
