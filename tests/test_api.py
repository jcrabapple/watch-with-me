from __future__ import annotations

import importlib.util
import logging
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_with_me import store

ROOT = Path(__file__).resolve().parent.parent


def build(tmp_db):
    spec = importlib.util.spec_from_file_location("wwm_api_under_test",
                                                  ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._conn = store.connect(tmp_db)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/watch-with-me")
    return module, TestClient(app)


@pytest.fixture()
def api(tmp_path):
    return build(tmp_path / "data.db")


def test_page_carries_a_strict_csp(api):
    _, client = api
    response = client.get("/api/plugins/watch-with-me/app")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "app.js" in response.text


def test_static_assets_are_served(api):
    _, client = api
    js = client.get("/api/plugins/watch-with-me/app.js")
    assert js.status_code == 200
    assert "text/javascript" in js.headers["content-type"]
    css = client.get("/api/plugins/watch-with-me/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]


def test_idle_session_has_no_media(api):
    _, client = api
    body = client.get("/api/plugins/watch-with-me/session").json()
    assert body["state"] == "idle"
    assert body["claimed"] is False
    assert body["media"] is None


def test_credits_endpoint_returns_the_withheld_artifact(api):
    module, client = api
    conn = module._conn
    with conn:
        conn.execute("INSERT INTO item(rating_key,media_type,title,runtime_ms) "
                     "VALUES('9','movie','Something',6000000)")
        conn.execute(
            "INSERT INTO claim(rating_key,claim_id,text,source_name,source_url,"
            "reveal_state,unlock_anchor_ms,build_version) "
            "VALUES('9','f1','Zoe locket claim.','plex',NULL,'locked',3600000,1)")
        conn.execute(
            "INSERT INTO claim(rating_key,claim_id,text,source_name,source_url,"
            "reveal_state,unlock_anchor_ms,build_version) "
            "VALUES('9','f2','The killer claim.','plex',NULL,'credits',NULL,1)")
    response = client.get("/api/plugins/watch-with-me/credits", params={"rating_key": "9"})
    assert response.status_code == 200
    claims = response.json()["claims"]
    # Locked entries come first with their unlock time; credits entries last.
    assert [c["reveal_state"] for c in claims] == ["locked", "credits"]
    assert claims[0]["unlock_anchor_ms"] == 3600000
    assert claims[0]["source_name"] == "plex"


def test_credits_endpoint_without_a_key_returns_empty(api):
    _, client = api
    body = client.get("/api/plugins/watch-with-me/credits").json()
    assert body == {"claims": []}


def test_delivered_message_appears_in_the_stream(api):
    module, client = api
    conn = module._conn
    with conn:
        conn.execute("INSERT INTO item(rating_key,media_type,title,runtime_ms) "
                     "VALUES('9','movie','Something',6000000)")
        conn.execute("INSERT INTO message(id,rating_key,t_ms,kind,text,build_version) "
                     "VALUES('m1','9',1000,'cast','First fact.',1)")
        conn.execute(
            "INSERT INTO session(session_id,rating_key,state,claimed,offset_ms,"
            "base_offset_ms,base_at_monotonic,last_seen_at,rebase_threshold_ms) "
            "VALUES('s1','9','live',1,5000,0,0,0,20600)"
        )
        conn.execute("INSERT INTO delivery(session_id,message_id,delivered_at,state) "
                     "VALUES('s1','m1',1,'shown')")
        conn.execute("INSERT INTO delivery(session_id,message_id,delivered_at,state) "
                     "VALUES('s1','m2',2,'skipped')")

    body = client.get("/api/plugins/watch-with-me/messages").json()
    assert [m["id"] for m in body["messages"]] == ["m1"]
    assert body["messages"][0]["text"] == "First fact."


def test_control_mutes_the_claimed_session(api):
    module, client = api
    conn = module._conn
    with conn:
        conn.execute(
            "INSERT INTO session(session_id,rating_key,state,claimed,offset_ms,"
            "base_offset_ms,base_at_monotonic,last_seen_at,rebase_threshold_ms) "
            "VALUES('s1','9','live',1,5000,0,0,0,20600)"
        )
    assert client.post("/api/plugins/watch-with-me/control",
                       json={"action": "mute"}).json() == {"ok": True}
    assert client.get("/api/plugins/watch-with-me/session").json()["muted"] is True


def test_feedback_rejects_unknown_actions(api):
    _, client = api
    response = client.post("/api/plugins/watch-with-me/feedback",
                           json={"action": "nonsense", "message_id": "m1"})
    assert response.status_code == 400


def test_control_end_sticks_and_suppresses_the_session(api):
    module, client = api
    conn = module._conn
    with conn:
        conn.execute(
            "INSERT INTO session(session_id,rating_key,state,claimed,offset_ms,"
            "base_offset_ms,base_at_monotonic,last_seen_at,rebase_threshold_ms) "
            "VALUES('s1','9','live',1,5000,0,0,0,20600)"
        )
    assert client.post("/api/plugins/watch-with-me/control",
                       json={"action": "end"}).json() == {"ok": True}
    session = store.latest_session(conn)
    assert session["state"] == "ended"
    assert session["claimed"] == 0
    assert store.get_state(conn, "claimed_session") == ""
    assert store.get_state(conn, "suppressed_session") == "s1"


def test_settings_endpoint_reports_resolved_model(api, monkeypatch):
    from watch_with_me import models
    monkeypatch.setattr(models, "load_hermes_config", lambda: {
        "auxiliary": {"watch_with_me_cues": {"model": "z-ai/glm-5.3-flash"}},
    })
    _, client = api
    body = client.get("/api/plugins/watch-with-me/settings").json()
    assert body["model"] == "z-ai/glm-5.3-flash"
    assert body["model_source"] == "picker"
    assert body["model_override_allowed"] is False
    assert body["guidance"]


def test_settings_endpoint_not_configured_state(api, monkeypatch):
    from watch_with_me import models
    monkeypatch.setattr(models, "load_hermes_config", lambda: {})
    _, client = api
    body = client.get("/api/plugins/watch-with-me/settings").json()
    assert body["model"] == ""
    assert body["model_source"] == "none"
    assert "auxiliary.watch_with_me_cues" in body["guidance"]


# -- Plex onboarding endpoints -----------------------------------------------------

from watch_with_me import onboarding as onboarding_mod  # noqa: E402

SETUP_RESOURCES = [
    {"name": "NAS", "clientIdentifier": "mach-1", "owned": True,
     "provides": "server",
     "connections": [
         {"uri": "http://172.17.0.2:32400", "local": True, "relay": False},
         {"uri": "http://192.168.1.20:32400", "local": True, "relay": False},
     ]},
]

FLOW_TOKEN = "tok-live-flow"


def wait_for_payload(client, phase, timeout=5.0):
    import time as time_mod
    deadline = time_mod.time() + timeout
    body = {}
    while time_mod.time() < deadline:
        body = client.get("/api/plugins/watch-with-me/setup").json()
        if body.get("phase") == phase:
            return body
        time_mod.sleep(0.05)
    raise AssertionError(f"setup flow never reached {phase}: {body}")


def patch_link_flow(monkeypatch, verify=None, hold: threading.Event | None = None):
    """Take the whole link flow offline; ``verify`` may raise to test failures.

    ``hold`` gates the fake poll so a test can observe the ``awaiting_link``
    phase before letting the flow continue.
    """
    def fake_poll(pid, cid, **kw):
        if hold is not None:
            hold.wait(5)
        return FLOW_TOKEN

    monkeypatch.setattr(onboarding_mod, "create_pin",
                        lambda cid: {"id": "1", "code": "K7Q2", "expires_at": 0})
    monkeypatch.setattr(onboarding_mod, "poll_pin_token", fake_poll)
    monkeypatch.setattr(onboarding_mod, "fetch_resources",
                        lambda tok, cid: SETUP_RESOURCES)
    monkeypatch.setattr(onboarding_mod, "probe_connection",
                        lambda uri, tok, cid, **kw: "192.168." in uri)
    monkeypatch.setattr(onboarding_mod, "verify_server",
                        verify or (lambda uri, tok, cid, **kw: {
                            "library_count": 2,
                            "library_names": ["Movies", "TV Shows"],
                            "readiness": {"sampled": 10, "with_sidecar": 9,
                                          "items": [], "note": "9 of 10 — fine."},
                        }))


def test_setup_endpoint_unlinked(api, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    _, client = api
    body = client.get("/api/plugins/watch-with-me/setup").json()
    assert body == {"linked": False, "link": "https://plex.tv/link",
                    "phase": "idle", "code": None, "server": None, "error": None}
    # asking for state must not create any onboarding artifacts
    assert not (tmp_path / "plex-credentials.json").exists()
    assert not (tmp_path / "plex-client-id").exists()


def test_setup_pin_flow_completes_and_never_exposes_the_token(api, tmp_path, monkeypatch):
    import json as json_mod
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    _, client = api
    hold = threading.Event()
    patch_link_flow(monkeypatch, hold=hold)

    started = client.post("/api/plugins/watch-with-me/setup/pin").json()
    assert started["phase"] == "awaiting_link"
    assert started["link"] == "https://plex.tv/link"
    hold.set()

    body = wait_for_payload(client, "done")
    assert body["linked"] is True
    server = body["server"]
    assert server["name"] == "NAS"
    assert server["access"] == "your server"
    assert server["connection"] == "http://192.168.1.20:32400"
    assert server["relay_fallback"] is False
    assert server["library_count"] == 2
    assert server["library_names"] == ["Movies", "TV Shows"]
    assert server["readiness"]["note"]

    # The docker-bridge URI lost the probe; the token is nowhere in the payload.
    assert "172.17.0.2" not in json_mod.dumps(body)
    assert FLOW_TOKEN not in json_mod.dumps(body)

    creds_file = tmp_path / "plex-credentials.json"
    assert creds_file.exists()
    assert creds_file.stat().st_mode & 0o777 == 0o600
    creds = json_mod.loads(creds_file.read_text("utf-8"))
    assert creds["token"] == FLOW_TOKEN
    assert creds["server_name"] == "NAS"
    assert creds["machine_id"] == "mach-1"
    assert creds["client_id"]  # generated per install and persisted


def test_setup_flow_401_surfaces_relink_message_and_saves_nothing(api, tmp_path,
                                                                  monkeypatch, caplog):
    import json as json_mod
    from watch_with_me.plex import PlexError
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)

    def verify_401(uri, tok, cid, **kw):
        raise PlexError("Plex HTTP 401 for /library/sections", status=401)

    _, client = api
    hold = threading.Event()
    with caplog.at_level(logging.WARNING):
        patch_link_flow(monkeypatch, verify=verify_401, hold=hold)
        client.post("/api/plugins/watch-with-me/setup/pin")
        body_started = client.get("/api/plugins/watch-with-me/setup").json()
        assert body_started["code"] == "K7Q2"
        hold.set()
        body = wait_for_payload(client, "error")

    assert body["error"] == onboarding_mod.RELINK_MESSAGE
    assert body["linked"] is False
    assert FLOW_TOKEN not in json_mod.dumps(body)
    assert not (tmp_path / "plex-credentials.json").exists()
    # the failure log line is the clean message, not the token
    assert FLOW_TOKEN not in caplog.text
    assert onboarding_mod.RELINK_MESSAGE in caplog.text


def test_setup_linked_state_reads_credentials(api, tmp_path, monkeypatch):
    import json as json_mod
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    onboarding_mod.save_credentials(
        base_url="http://192.168.1.20:32400", token=FLOW_TOKEN, client_id="cid-9",
        server_name="NAS", machine_id="mach-1",
        path=tmp_path / "plex-credentials.json")

    class StubClient:
        def __init__(self, *a, **k):
            pass

        def library_sections(self):
            return [{"title": "Movies"}, {"title": "TV Shows"}]

        def recently_added(self, limit=10):
            return []

        def metadata(self, rating_key):
            return {}

    module, client = api
    monkeypatch.setattr(module, "PlexClient", StubClient)
    body = client.get("/api/plugins/watch-with-me/setup").json()
    assert body["linked"] is True
    assert body["server"]["name"] == "NAS"
    assert body["server"]["library_count"] == 2
    assert body["error"] is None
    assert FLOW_TOKEN not in json_mod.dumps(body)


def test_setup_linked_state_401_from_stored_token(api, tmp_path, monkeypatch):
    from watch_with_me.plex import PlexError
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    onboarding_mod.save_credentials(
        base_url="http://192.168.1.20:32400", token=FLOW_TOKEN, client_id="cid-9",
        server_name="NAS", machine_id="mach-1",
        path=tmp_path / "plex-credentials.json")

    class StaleTokenClient:
        def __init__(self, *a, **k):
            pass

        def library_sections(self):
            raise PlexError("Plex HTTP 401 for /library/sections", status=401)

    module, client = api
    monkeypatch.setattr(module, "PlexClient", StaleTokenClient)
    body = client.get("/api/plugins/watch-with-me/setup").json()
    assert body["error"] == onboarding_mod.RELINK_MESSAGE
    assert body["server"]["name"] == "NAS"  # stored info still shown


def test_setup_cancel_returns_flow_to_idle(api, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    _, client = api
    hold = threading.Event()
    patch_link_flow(monkeypatch, hold=hold)
    client.post("/api/plugins/watch-with-me/setup/pin")
    body = client.post("/api/plugins/watch-with-me/setup/cancel").json()
    assert body == {"ok": True}
    hold.set()  # release the worker so the daemon thread does not linger


SRT_TWO_CUES = (
    "1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"
    "2\n00:00:05,000 --> 00:00:07,000\nGeneral Kenobi.\n"
)


def test_setup_payload_carries_download_consent_block(api, tmp_path, monkeypatch):
    from watch_with_me.config import DOWNLOAD_SUBTITLES_HELP

    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    onboarding_mod.save_credentials(
        base_url="http://192.168.1.20:32400", token=FLOW_TOKEN, client_id="cid-9",
        server_name="NAS", machine_id="mach-1",
        path=tmp_path / "plex-credentials.json")

    class StubClient:
        def __init__(self, *a, **k):
            pass

        def library_sections(self):
            return [{"title": "Movies"}]

        def recently_added(self, limit=10):
            return []

        def metadata(self, rating_key):
            return {}

    module, client = api
    monkeypatch.setattr(module, "PlexClient", StubClient)
    body = client.get("/api/plugins/watch-with-me/setup").json()
    download = body["server"]["download"]
    assert download["enabled"] is False  # opt-in default
    assert download["languages"] == ["en"]
    assert download["budget"] == 5
    assert download["help"] == DOWNLOAD_SUBTITLES_HELP
    assert "Plex Pass" in download["help"] and "cached copy" in download["help"]
    assert FLOW_TOKEN not in str(body)


def test_setup_subtitles_download_per_item_action(api, tmp_path, monkeypatch):
    """The per-item action is itself the consent: it attaches one subtitle
    even though the automatic setting is off (its default)."""
    from watch_with_me import subtitles as subtitles_mod

    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    onboarding_mod.save_credentials(
        base_url="http://192.168.1.20:32400", token=FLOW_TOKEN, client_id="cid-9",
        server_name="NAS", machine_id="mach-1",
        path=tmp_path / "plex-credentials.json")

    class DownloadClient:
        def __init__(self, *a, **k):
            self.attached = None

        def metadata(self, rating_key):
            streams = [{"id": 100, "streamType": 3, "codec": "srt",
                        "languageCode": "eng"}]
            if self.attached:
                streams.append({"id": 99745, "streamType": 3, "codec": "srt",
                                "languageCode": "eng",
                                "key": "/library/streams/99745"})
            return {"ratingKey": str(rating_key), "type": "movie",
                    "Media": [{"Part": [{"Stream": streams}]}]}

        def search_subtitles(self, rating_key, language, *, forced=0,
                             hearing_impaired=0):
            return [{"id": 99745, "codec": "srt", "languageCode": "eng",
                     "key": "/library/streams/99745", "score": 18}]

        def attach_subtitle(self, rating_key, key, codec="srt"):
            self.attached = (rating_key, key, codec)

        def sidecar(self, stream_key, extension="srt"):
            return SRT_TWO_CUES

    module, client = api
    fake = DownloadClient()
    monkeypatch.setattr(module, "PlexClient", lambda *a, **k: fake)
    response = client.post("/api/plugins/watch-with-me/setup/subtitles/download",
                           json={"rating_key": "42"})
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "downloaded"
    assert body["ok"] is True and body["cues"] == 2
    assert fake.attached == ("42", "/library/streams/99745", "srt")
    # the text is cached in the (redirected) plugin data dir, token absent
    cached = (tmp_path / "subtitles" / "42-en.srt").read_text("utf-8")
    assert cached == SRT_TWO_CUES
    assert FLOW_TOKEN not in cached
    assert subtitles_mod.read_cache(module._conn, "42", "en") is not None


def test_setup_subtitles_download_requires_rating_key(api, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
    _, client = api
    response = client.post("/api/plugins/watch-with-me/setup/subtitles/download",
                           json={})
    assert response.status_code == 400
