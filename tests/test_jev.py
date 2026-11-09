"""Optional Jev entity selection: key handling, provider resolution, strict
answer validation, fail-open behaviour, and the Expander seam.

Every test here uses a fake transport or a stub completer — no network, no
live keys. store.data_dir() is monkeypatched to a temp dir so no test ever
touches the real plugin data dir.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_title  # noqa: E402
from watch_with_me import entities, jev  # noqa: E402
from watch_with_me import store  # noqa: E402
from watch_with_me.config import Settings  # noqa: E402

KEY = "sk-test-4f8a2b7c9d1e4f8a2b7c9d1e4f8a2b7"

LINES = [
    {"start_ms": 1000, "end_ms": 3000,
     "text": "They drove all the way to New Mexico that night."},
    {"start_ms": 5000, "end_ms": 7000,
     "text": "The desert went on for miles past the Trinity site."},
]
SEGMENT = {"start_ms": 0, "end_ms": 10_000, "lines": LINES}

CANDIDATES = [
    {"name": "New Mexico", "why": "mentioned in dialogue"},
    {"name": "Trinity", "why": "mentioned in dialogue"},
    {"name": "Osage orange", "why": "mentioned in dialogue"},
]


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    target = tmp_path / "plugin-data"
    target.mkdir()
    monkeypatch.setattr(store, "data_dir", lambda: target)
    return target


@pytest.fixture
def standalone(monkeypatch):
    """Simulate a process with no host: ``agent.secret_scope`` unimportable."""
    monkeypatch.setattr(jev, "_host_secret", lambda name: None)


@pytest.fixture
def host_scope(monkeypatch):
    """Simulate the gateway: the host's secret scope answers, and its answer is
    the only one that counts."""
    def scope(name, value=""):
        monkeypatch.setattr(jev, "_host_secret",
                            lambda n: (value if n == name else ""))
    return scope



def answer_payload(choice: str, probabilities: dict) -> bytes:
    return json.dumps({
        "id": "req_1", "model": "typesafe/jev-1.13", "provider": "openrouter",
        "answers": {"entity_selection": {"choice": choice,
                                         "probabilities": probabilities}},
    }).encode()


def transport_returning(status: int, body: bytes) -> jev.Transport:
    calls: list[tuple] = []

    def fake(url, headers, body_bytes, timeout):
        calls.append((url, headers, body_bytes, timeout))
        return status, body

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def transport_raising(exc: Exception) -> jev.Transport:
    def fake(url, headers, body_bytes, timeout):
        raise exc

    return fake


def enabled_settings(**overrides) -> Settings:
    base = {"jev_enabled": True, "jev_provider": "openrouter",
            "jev_api_key": KEY}
    base.update(overrides)
    return Settings(**base)


# --- key storage ---------------------------------------------------------------

def test_save_and_load_api_key_round_trip(data_dir):
    path = jev.save_api_key(KEY)
    assert path == data_dir / jev.KEY_FILE
    assert (path.stat().st_mode & 0o777) == 0o600
    assert jev.load_api_key() == KEY


def test_load_api_key_missing_or_corrupt(data_dir):
    assert jev.load_api_key() == ""
    bad = data_dir / jev.KEY_FILE
    bad.write_text("not json", encoding="utf-8")
    assert jev.load_api_key() == ""


def test_key_status_is_masked(data_dir):
    assert jev.key_status() == {"configured": False, "last4": None}
    jev.save_api_key(KEY)
    assert jev.key_status() == {"configured": True, "last4": KEY[-4:]}


def test_save_api_key_rejects_empty(data_dir):
    with pytest.raises(ValueError):
        jev.save_api_key("   ")


# --- provider / key resolution ---------------------------------------------------

def test_resolve_disabled_returns_none(data_dir):
    assert jev.resolve(Settings(), env={}) is None


def test_resolve_no_key_means_disabled_not_a_crash(data_dir, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings(jev_enabled=True)  # no key anywhere
    assert jev.resolve(settings, env={}) is None
    # And the selector factory follows: None, silently.
    assert jev.make_selector(settings, env={}) is None


def test_explicit_setting_key_beats_env(data_dir, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-should-lose")
    config = jev.resolve(enabled_settings(jev_api_key=KEY), env=os.environ)
    assert config is not None
    assert config.api_key == KEY
    assert config.provider == "openrouter"


def test_auto_prefers_explicit_key(data_dir):
    config = jev.resolve(enabled_settings(), env={})
    assert config is not None
    assert config.api_key == KEY
    # An explicit key routes to the OpenRouter transport by default; a
    # direct TypeSafe key requires jev_provider=typesafe.
    assert config.provider == "openrouter"
    assert config.base_url == jev.OPENROUTER_BASE_URL


def test_auto_explicit_key_can_target_typesafe(data_dir):
    config = jev.resolve(enabled_settings(jev_provider="typesafe"), env={})
    assert config is not None
    assert config.provider == "typesafe"
    assert config.base_url == jev.TYPESAFE_BASE_URL
    assert config.model == jev.TYPESAFE_MODEL
    assert config.path == jev.TYPESAFE_PATH


def test_auto_picks_typesafe_env_then_openrouter_env(data_dir, monkeypatch, standalone):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-typesafe-env")
    config = jev.resolve(Settings(jev_enabled=True), env=os.environ)
    assert config is not None and config.provider == "typesafe" \
        and config.api_key == "sk-typesafe-env"

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-env")
    config = jev.resolve(Settings(jev_enabled=True), env=os.environ)
    assert config is not None and config.provider == "openrouter" \
        and config.api_key == "sk-openrouter-env"


def test_explicit_provider_uses_its_own_env_var(data_dir, monkeypatch, standalone):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-env")
    settings = Settings(jev_enabled=True, jev_provider="typesafe")
    config = jev.resolve(settings, env=os.environ)
    assert config is None  # typesafe chosen, typesafe env absent: disabled


def test_gateway_scope_answer_is_final_even_when_empty(data_dir, monkeypatch, host_scope):
    """No env fallback in the gateway: the launch profile's key must not leak."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-launch-profile")
    host_scope("OPENROUTER_API_KEY", "")
    assert jev.resolve(Settings(jev_enabled=True), env=os.environ) is None


def test_gateway_scoped_key_beats_launch_env(data_dir, monkeypatch, host_scope):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-launch-profile")
    host_scope("OPENROUTER_API_KEY", "sk-scoped")
    config = jev.resolve(Settings(jev_enabled=True), env=os.environ)
    assert config is not None
    assert config.api_key == "sk-scoped"


def test_host_secret_returns_none_without_a_host(monkeypatch):
    """``None`` (standalone) and ``""`` (host has no value) are different."""
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    assert jev._host_secret("OPENROUTER_API_KEY") is None


def test_base_urls_are_not_env_overridable(data_dir, standalone, monkeypatch):
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://127.0.0.1:9")
    config = jev.resolve(enabled_settings(), env=os.environ)
    assert config.base_url == jev.OPENROUTER_BASE_URL


def test_model_default_and_override(data_dir):
    config = jev.resolve(enabled_settings(), env={})
    assert config.model == jev.OPENROUTER_MODEL
    config = jev.resolve(enabled_settings(jev_model="custom/jev-x"), env={})
    assert config.model == "custom/jev-x"


def test_resolve_tolerates_garbage_provider(data_dir):
    config = jev.resolve(enabled_settings(jev_provider="banana"), env={})
    assert config is not None and config.provider == "openrouter"


# --- strict answer validation ----------------------------------------------------

def _client(transport) -> jev.Client:
    config = jev.Config("openrouter", KEY, jev.OPENROUTER_MODEL,
                        jev.OPENROUTER_BASE_URL)
    return jev.Client(config, transport=transport, backoff=(0,))


CRITERIA = {"c0": "New Mexico", "c1": "Trinity", jev.NONE_ID: "Nothing"}


def _decide(client, choice, probabilities):
    return client.decide(state={}, question_id="entity_selection",
                         criteria=CRITERIA, goal="g", rules=["r"])


def test_choice_not_in_criteria_is_rejected():
    transport = transport_returning(
        200, answer_payload("c9", {"c0": 0.5, "c1": 0.3, "NONE": 0.2}))
    with pytest.raises(jev.JevError):
        _decide(_client(transport), "c9", {})


def test_probabilities_missing_an_offered_id_is_rejected():
    transport = transport_returning(
        200, answer_payload("c0", {"c0": 0.8, "c1": 0.2}))  # no NONE
    with pytest.raises(jev.JevError):
        _decide(_client(transport), "c0", {})


def test_argmax_mismatch_is_rejected():
    transport = transport_returning(
        200, answer_payload("c0", {"c0": 0.4, "c1": 0.5, "NONE": 0.1}))
    with pytest.raises(jev.JevError):
        _decide(_client(transport), "c0", {})


def test_valid_answer_is_accepted():
    transport = transport_returning(
        200, answer_payload("c0", {"c0": 0.7, "c1": 0.2, "c2": 0.05, "NONE": 0.05}))
    decision = _decide(_client(transport), "c0", {})
    assert decision.choice == "c0"
    assert decision.probabilities["c0"] == 0.7
    assert decision.provider == "openrouter"


def test_http_500_raises():
    transport = transport_returning(500, b"boom")
    with pytest.raises(jev.JevError):
        _decide(_client(transport), "c0", {})


def test_malformed_json_raises():
    transport = transport_returning(200, b"{not json")
    with pytest.raises(jev.JevError):
        _decide(_client(transport), "c0", {})


def test_transport_error_raises_after_retry():
    transport = transport_raising(OSError("connection refused"))
    client = _client(transport)
    client._retries = 1
    with pytest.raises(jev.JevError):
        _decide(client, "c0", {})


def test_retry_recovers_on_second_attempt():
    attempts = {"n": 0}

    def flaky(url, headers, body, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("timeout")
        return 200, answer_payload("c0", {"c0": 0.7, "c1": 0.2, "NONE": 0.1})

    client = _client(flaky)
    client._retries = 1
    decision = _decide(client, "c0", {})
    assert decision.choice == "c0"
    assert attempts["n"] == 2


# --- the selection question shape -------------------------------------------------

def test_request_shape_uses_a_criteria_record(data_dir):
    transport = transport_returning(
        200, answer_payload("c0", {"c0": 0.7, "c1": 0.2, "c2": 0.05, "NONE": 0.05}))
    config = jev.resolve(enabled_settings(), env={})
    client = jev.Client(config, transport=transport, backoff=(0,))
    picked = jev.select_entities(client, CANDIDATES, SEGMENT,
                                 log_path=data_dir / "log.jsonl")
    assert picked == CANDIDATES[:2]  # top-2 by probability
    url, headers, body, timeout = transport.calls[0]  # type: ignore[attr-defined]
    assert url == jev.OPENROUTER_BASE_URL + jev.OPENROUTER_PATH
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert timeout == jev.DEFAULT_TIMEOUT
    payload = json.loads(body)
    assert payload["model"] == jev.OPENROUTER_MODEL
    question = payload["questions"]["entity_selection"]
    assert question["type"] == "choice"
    assert isinstance(question["criteria"], dict)  # a record, never an array
    assert set(question["criteria"]) == {"c0", "c1", "c2", "NONE"}
    assert "transcript" in payload["state"]


# --- select_entities outcomes -----------------------------------------------------

def test_none_win_suppresses_everything(data_dir):
    transport = transport_returning(
        200, answer_payload("NONE", {"c0": 0.1, "c1": 0.1, "c2": 0.1, "NONE": 0.7}))
    config = jev.resolve(enabled_settings(), env={})
    client = jev.Client(config, transport=transport, backoff=(0,))
    assert jev.select_entities(client, CANDIDATES, SEGMENT,
                               log_path=data_dir / "log.jsonl") == []


def test_ambiguous_band_returns_none_for_the_heuristic(data_dir):
    transport = transport_returning(
        200, answer_payload("c0", {"c0": 0.5, "c1": 0.3, "c2": 0.1, "NONE": 0.1}))
    config = jev.resolve(enabled_settings(), env={})
    client = jev.Client(config, transport=transport, backoff=(0,))
    assert jev.select_entities(client, CANDIDATES, SEGMENT,
                               log_path=data_dir / "log.jsonl") is None


@pytest.mark.parametrize("bad", [
    transport_raising(OSError("no route to host")),
    transport_returning(500, b"server error"),
    transport_returning(200, b"not json at all"),
])
def test_fail_open_paths_return_none(data_dir, bad):
    config = jev.resolve(enabled_settings(), env={})
    client = jev.Client(config, transport=bad, backoff=(0,))
    assert jev.select_entities(client, CANDIDATES, SEGMENT,
                               log_path=data_dir / "log.jsonl") is None


def test_every_outcome_is_logged_without_key_material(data_dir):
    transport = transport_returning(
        200, answer_payload("NONE", {"c0": 0.1, "c1": 0.1, "c2": 0.1, "NONE": 0.7}))
    config = jev.resolve(enabled_settings(), env={})
    client = jev.Client(config, transport=transport, backoff=(0,))
    jev.select_entities(client, CANDIDATES, SEGMENT)
    log_path = data_dir / jev.DECISIONS_LOG
    raw = log_path.read_text("utf-8")
    record = json.loads(raw.splitlines()[-1])
    assert record["choice"] == "NONE"
    assert record["outcome"] == "ok"
    assert record["provider"] == "openrouter"
    assert "latency_ms" in record
    assert KEY not in raw
    assert KEY[-4:] not in raw  # even the last4 stays out of the log


def test_log_rotates_at_the_cap(data_dir, monkeypatch):
    monkeypatch.setattr(jev, "LOG_CAP_BYTES", 64)
    log_path = data_dir / jev.DECISIONS_LOG
    log_path.write_text("x" * 100, encoding="utf-8")
    jev.log_decision({"ts": 1, "q": "entity_selection"}, path=log_path)
    assert (data_dir / (jev.DECISIONS_LOG + ".1")).exists()
    lines = [json.loads(l) for l in
             log_path.read_text("utf-8").splitlines() if l.strip()]
    assert lines[-1]["q"] == "entity_selection"


# --- the Expander seam -------------------------------------------------------------

ANCHOR = LINES[0]["text"]


class FullCompleter:
    """Serves the extraction call and the note-writing call; records prompts."""

    def __init__(self, *names):
        self.names = names
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "You identify real-world entities" in prompt:
            return json.dumps({"entities": [
                {"name": n, "why": "mentioned"} for n in self.names]})
        if "You write one short trivia note" in prompt:
            return json.dumps({"messages": [{
                "anchor": ANCHOR,
                "text": "A grounded fact from the entity article.",
                "confidence": 0.9}]})
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")


def _fake_fetch(name, **kwargs):
    return {"name": name, "url": f"https://en.wikipedia.org/wiki/{name}",
            "summary": f"{name} is a place with real substance worth noting.",
            "extract": f"{name} is a place with real substance worth noting."}


def _expand_with_selector(selector, completer, tmp_path, monkeypatch,
                          names=("New Mexico", "Trinity")):
    monkeypatch.setattr(entities, "fetch_summary", _fake_fetch)
    if completer is None:
        completer = FullCompleter(*names)
    expander = entities.Expander(completer,
                                 cache=entities.EntityCache(tmp_path / "c.json"),
                                 selector=selector)
    notes = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    return expander, completer, notes


def test_selector_prompt_widens_the_candidate_budget(tmp_path, monkeypatch):
    selector = lambda candidates, segment: candidates[:1]  # noqa: E731
    _, completer, notes = _expand_with_selector(
        selector, None, tmp_path, monkeypatch,
        names=("New Mexico", "Trinity", "Roswell", "White Sands", "Albuquerque"))
    assert "At most 5 entities." in completer.prompts[0]
    assert len(notes) == 1  # only the chosen one was offered for fetching


def test_none_win_suppresses_the_segment(tmp_path, monkeypatch, data_dir):
    transport = transport_returning(
        200, answer_payload("NONE", {"c0": 0.05, "c1": 0.05, "c2": 0.05, "NONE": 0.85}))
    selector = jev.make_selector(enabled_settings(), env={},
                                 transport=transport)
    expander, _, notes = _expand_with_selector(selector, None, tmp_path,
                                               monkeypatch)
    assert notes == []
    assert expander.stats["jev_none"] == 1


def test_unreachable_jev_falls_back_to_the_heuristic(tmp_path, monkeypatch,
                                                   data_dir):
    fetches: list[str] = []

    def counting_fetch(name, **kwargs):
        fetches.append(name)
        return _fake_fetch(name)

    monkeypatch.setattr(entities, "fetch_summary", counting_fetch)
    transport = transport_raising(OSError("unreachable"))
    selector = jev.make_selector(enabled_settings(), env={},
                                 transport=transport)
    expander = entities.Expander(FullCompleter("New Mexico", "Trinity"),
                                 cache=entities.EntityCache(tmp_path / "c.json"),
                                 selector=selector)
    notes = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    assert len(notes) == 2  # heuristic: both candidates fetched
    assert expander.stats["jev_fallback"] == 1


def test_jev_disabled_is_byte_identical_to_no_jev(tmp_path, monkeypatch,
                                                  data_dir):
    """Kill switch: default settings + no key => selector None => the same
    prompts, the same entity set, and the same stats shape as before Jev."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings(entity_expansion=True)  # jev_enabled defaults False

    monkeypatch.setattr(entities, "fetch_summary", _fake_fetch)
    plain_completer = FullCompleter("New Mexico", "Trinity")
    plain = entities.Expander(plain_completer,
                              cache=entities.EntityCache(tmp_path / "plain.json"))
    plain_notes = plain.expand(SEGMENT, "transcript", pass_a_count=0)

    wired = build_title.build_expander(settings, FullCompleter(
        "New Mexico", "Trinity"), {}, wikipedia=True)
    assert wired is not None and wired.selector is None
    jev_notes = wired.expand(SEGMENT, "transcript", pass_a_count=0)

    assert json.dumps(jev_notes, sort_keys=True) \
        == json.dumps(plain_notes, sort_keys=True)
    assert wired.complete.prompts == plain_completer.prompts
    assert list(wired.stats.keys()) == ["calls", "fetches", "notes",
                                        "skipped_names", "unusable"]


def test_selector_exception_never_sinks_the_segment(tmp_path, monkeypatch):
    def boom(candidates, segment):
        raise RuntimeError("selector exploded")

    expander, _, notes = _expand_with_selector(boom, None, tmp_path,
                                               monkeypatch)
    assert len(notes) == 2  # heuristic fallback
    assert expander.stats["jev_fallback"] == 1


# --- the dashboard API: masked reads, write-only key field ------------------------

def _build_api(tmp_db):
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "wwm_api_jev_under_test", root / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._conn = store.connect(tmp_db)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/watch-with-me")
    return module, TestClient(app)


def test_settings_payload_masks_the_key(data_dir, tmp_path):
    jev.save_api_key(KEY)
    _, client = _build_api(tmp_path / "data.db")
    body = client.get("/api/plugins/watch-with-me/settings").json()
    assert body["jev"]["enabled"] is False          # default: off
    assert body["jev"]["provider"] == "auto"
    assert body["jev"]["configured"] is True
    assert body["jev"]["last4"] == KEY[-4:]
    assert KEY not in json.dumps(body)              # the key itself never ships


def test_settings_payload_reports_unconfigured(data_dir, tmp_path):
    _, client = _build_api(tmp_path / "data.db")
    body = client.get("/api/plugins/watch-with-me/settings").json()
    assert body["jev"]["configured"] is False
    assert body["jev"]["last4"] is None


def test_settings_jev_endpoint_writes_and_deletes(data_dir, tmp_path):
    _, client = _build_api(tmp_path / "data.db")
    response = client.post("/api/plugins/watch-with-me/settings/jev",
                           json={"api_key": KEY})
    assert response.status_code == 200
    body = response.json()
    assert body == {"configured": True, "last4": KEY[-4:]}
    stored = data_dir / jev.KEY_FILE
    assert (stored.stat().st_mode & 0o777) == 0o600
    assert KEY not in json.dumps(body)

    clear = client.post("/api/plugins/watch-with-me/settings/jev",
                        json={"api_key": ""})
    assert clear.json() == {"configured": False, "last4": None}
    assert not stored.exists()


def test_make_selector_with_no_key_returns_none_silently(data_dir, monkeypatch):
    """The orchestrator's acceptance test: enabled in settings but nothing
    configured anywhere — the build path gets None, no exception, no log."""
    import logging
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        selector = jev.make_selector(Settings(jev_enabled=True), env={})
    finally:
        root.removeHandler(handler)
    assert selector is None
    assert [r for r in records if r.name.startswith("watch_with_me")] == []
