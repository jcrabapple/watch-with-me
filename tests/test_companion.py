"""LAN companion URL discovery (watch_with_me/companion.py).

The rule: an explicit ``public_base_url`` setting wins verbatim; otherwise
the detected LAN IPv4 is combined with the request's scheme/port (or the
plugin default ``localhost:9119`` when there is no request, e.g. a chat
notification composed on a worker thread).
"""

from __future__ import annotations

from watch_with_me import companion
from watch_with_me.companion import companion_url
from watch_with_me.config import Settings

APP = "/api/plugins/watch-with-me/app"


class FakeURL:
    def __init__(self, scheme="http", port=None):
        self.scheme = scheme
        self.port = port


class FakeRequest:
    def __init__(self, scheme="http", port=None):
        self.url = FakeURL(scheme, port)


def test_public_base_url_wins_verbatim(monkeypatch):
    monkeypatch.setattr(companion, "lan_ipv4", lambda: "192.168.1.67")
    url = companion_url(FakeRequest("http", 9119),
                        Settings(public_base_url="https://wwm.example.test/"))
    assert url == "https://wwm.example.test" + APP


def test_public_base_url_with_an_invalid_scheme_is_ignored(monkeypatch):
    """A bad override (no http(s) scheme) must not produce a dead link:
    resolution falls through to LAN discovery exactly as when it is empty."""
    monkeypatch.setattr(companion, "lan_ipv4", lambda: "192.168.1.67")
    for bogus in ("wwm.example.test", "ftp://wwm.example.test",
                  "gopher://wwm.example.test:70/", "//wwm.example.test"):
        url = companion_url(FakeRequest("http", 9119),
                            Settings(public_base_url=bogus))
        assert url == "http://192.168.1.67:9119" + APP, bogus
    # http and https still win verbatim.
    assert companion_url(FakeRequest("http", 9119),
                         Settings(public_base_url="http://nas.local:8080")) == \
        "http://nas.local:8080" + APP


def test_lan_ipv4_combines_with_the_request_port_and_scheme(monkeypatch):
    monkeypatch.setattr(companion, "lan_ipv4", lambda: "192.168.1.67")
    url = companion_url(FakeRequest("http", 9119), Settings())
    assert url == "http://192.168.1.67:9119" + APP


def test_https_request_without_an_explicit_port_uses_443(monkeypatch):
    monkeypatch.setattr(companion, "lan_ipv4", lambda: "192.168.1.67")
    url = companion_url(FakeRequest("https", None), Settings())
    assert url == "https://192.168.1.67:443" + APP


def test_no_request_and_no_lan_falls_back_to_localhost_9119(monkeypatch):
    monkeypatch.setattr(companion, "lan_ipv4", lambda: None)
    assert companion_url(None, Settings()) == "http://localhost:9119" + APP


def test_lan_detection_failure_keeps_the_request_port(monkeypatch):
    monkeypatch.setattr(companion, "lan_ipv4", lambda: None)
    url = companion_url(FakeRequest("http", 8080), Settings())
    assert url == "http://localhost:8080" + APP
