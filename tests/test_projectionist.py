"""Ask the Projectionist: prompt gating, response parsing, engine behaviour.

The engine tests run on a temp DB with a stub completer and a stub frame
fetcher — never the live DB, never the network.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watch_with_me import projectionist, store
from watch_with_me.config import Settings

ROOT = Path(__file__).resolve().parent.parent

SRT = """\
1
00:09:50,000 --> 00:09:52,000
She picks up the locket.
 
2
00:09:58,000 --> 00:10:00,000
That is aconite, the poison.
 
3
00:11:40,000 --> 00:11:42,000
This line is after the playhead.
 
"""


# -- pure: playhead and window -------------------------------------------------

def test_playhead_predicts_forward_only_when_live():
    session = {"offset_ms": 600_000, "base_offset_ms": 600_000,
               "base_at_monotonic": 1000.0, "state": "live"}
    assert projectionist.playhead_ms(session, 1000.0) == 600_000
    assert projectionist.playhead_ms(session, 1012.5) == 612_500
    paused = dict(session, state="paused")
    assert projectionist.playhead_ms(paused, 1012.5) == 600_000
    legacy = {"offset_ms": 42_000, "base_offset_ms": 0, "base_at_monotonic": None,
              "state": "live"}
    assert projectionist.playhead_ms(legacy, 9999.0) == 42_000


def test_window_lines_never_pass_the_playhead():
    cues = [
        {"start_ms": 550_000, "end_ms": 551_000, "text": "too old"},
        {"start_ms": 571_000, "end_ms": 572_000, "text": "in window"},
        {"start_ms": 600_000, "end_ms": 601_000, "text": "at playhead"},
        {"start_ms": 600_500, "end_ms": 601_000, "text": "beyond playhead"},
    ]
    lines = projectionist.window_lines(cues, 600_000)
    assert [c["text"] for c in lines] == ["in window", "at playhead"]


# -- pure: escrow gating for notes (operator decision) --------------------------

def _note(**overrides):
    note = {"id": "m1", "kind": "cast", "text": "A note.",
            "source_name": "plex", "reveal_state": "live"}
    note.update(overrides)
    return note


def test_note_without_state_needs_its_subject_in_the_window():
    note = _note(reveal_state=None, subject_key="aconitum")
    out = projectionist.notes_for_ask([note], 600_000, "she picks up the locket")
    assert out == []
    out = projectionist.notes_for_ask([note], 600_000, "that is aconitum, the poison")
    assert out == [note]


def test_entity_note_is_window_checked_even_when_stored_live():
    # Entity notes persisted before the ledger carry reveal_state='live'
    # (the column default) though the escrow classifier never covered them.
    note = _note(kind="context", source_name="wikipedia:Aconitum",
                 reveal_state="live", subject_key=None)
    assert projectionist.notes_for_ask([note], 600_000, "no mention here") == []
    assert projectionist.notes_for_ask([note], 600_000, "aconitum on the table") == [note]


def test_locked_and_credits_notes_never_enter_an_ask():
    locked = _note(reveal_state="locked", unlock_anchor_ms=10_000)
    credits = _note(reveal_state="credits")
    live = _note()
    out = projectionist.notes_for_ask([locked, credits, live], 9_999_999, "anything")
    assert out == [live]


# -- pure: prompt assembly -------------------------------------------------------

def _claim(claim_id, state, anchor=None):
    return {"claim_id": claim_id, "text": f"Claim {claim_id} text.",
            "source_name": "plex", "source_url": None,
            "reveal_state": state, "unlock_anchor_ms": anchor}


def _ask_prompt(playhead=600_000):
    cues = [
        {"start_ms": 571_000, "end_ms": 572_000, "text": "She picks up the locket."},
        {"start_ms": 600_500, "end_ms": 601_000, "text": "Beyond the playhead."},
    ]
    claims = [_claim("f1", "live"), _claim("f2", "locked", 3_600_000)]
    visible = [c for c in claims
               if c["reveal_state"] == "live"
               or (c["unlock_anchor_ms"] or 0) <= playhead]
    return projectionist.build_ask_prompt(
        title="Something", playhead=playhead, lines=projectionist.window_lines(cues, playhead),
        claims=visible, notes=[], has_frame=False)


def test_prompt_has_only_elapsed_transcript():
    prompt = _ask_prompt()
    assert "She picks up the locket." in prompt
    assert "Beyond the playhead." not in prompt


def test_claim_unlocked_at_minute_60_is_absent_from_a_minute_10_ask():
    prompt = _ask_prompt(playhead=600_000)
    assert "f1 [plex]" in prompt
    assert "f2 [plex]" not in prompt
    later = _ask_prompt(playhead=3_660_000)
    assert "f2 [plex]" in later


def test_prompt_says_when_no_frame_is_available():
    assert "No frame is available" in _ask_prompt()
    framed = projectionist.build_ask_prompt(
        title="T", playhead=1000, lines=[], claims=[], notes=[], has_frame=True)
    assert "attached image" in framed


# -- pure: response parsing --------------------------------------------------------

def test_parse_answered():
    raw = json.dumps({"answer": "The locket is aconite.", "fact_ids": ["f1"],
                      "confidence": 0.9})
    out = projectionist.parse_ask_response(raw, {"f1"})
    assert out["status"] == "answered"
    assert out["answer"] == "The locket is aconite."
    assert out["fact_ids"] == ["f1"]
    assert out["confidence"] == 0.9


def test_parse_nothing_solid():
    out = projectionist.parse_ask_response(
        json.dumps({"answer": None, "reason": "nothing_solid"}), {"f1"})
    assert out == {"status": "nothing", "reason": "nothing_solid"}


def test_parse_malformed_json_degrades_to_nothing():
    assert projectionist.parse_ask_response("not json at all", {"f1"})["status"] == "nothing"
    assert projectionist.parse_ask_response("", {"f1"})["status"] == "nothing"
    assert projectionist.parse_ask_response('{"answer": 5}', {"f1"})["status"] == "nothing"


def test_parse_rejects_a_fact_id_that_was_not_supplied():
    raw = json.dumps({"answer": "Leak attempt.", "fact_ids": ["f99"],
                      "confidence": 0.9})
    out = projectionist.parse_ask_response(raw, {"f1"})
    assert out["status"] == "nothing"


def test_cache_key_buckets_and_orders():
    a = projectionist.cache_key("9", 600_001, ["f2", "f1"], ["m1"], True)
    b = projectionist.cache_key("9", 614_999, ["f1", "f2"], ["m1"], True)
    c = projectionist.cache_key("9", 615_000, ["f1", "f2"], ["m1"], True)
    assert a == b  # same 15s bucket, ids sorted
    assert a != c  # next bucket
    assert a != projectionist.cache_key("9", 600_001, ["f1", "f2"], ["m1"], False)


# -- engine ------------------------------------------------------------------------

class Clock:
    def __init__(self, t=1000.0):
        self.t = t
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.t

    def advance(self, seconds):
        with self.lock:
            self.t += seconds


class Completer:
    """Records calls; optionally blocks the first call until released."""

    def __init__(self, answer=None, block=None):
        self.calls = []
        self.answer = answer or json.dumps(
            {"answer": "It is aconite.", "fact_ids": ["f1"], "confidence": 0.8})
        self.block = block

    def __call__(self, prompt, frame, mime):
        self.calls.append({"prompt": prompt, "frame": frame, "mime": mime})
        if self.block is not None:
            self.block.wait(10)
        return self.answer


class Frames:
    def __init__(self, supported=False, fail=False):
        self.supported = supported
        self.fail = fail
        self.fetches = []

    def probe(self, rating_key):
        return self.supported

    def fetch(self, rating_key, ms):
        self.fetches.append(ms)
        if self.fail:
            raise RuntimeError("plex down")
        return b"\xff\xd8fakejpeg", "image/jpeg"


class Transcripts:
    def __init__(self, text=None, fail=False):
        self.text = text
        self.fail = fail
        self.fetches = []

    def fetch(self, rating_key):
        self.fetches.append(rating_key)
        if self.fail:
            raise RuntimeError("plex down")
        return self.text


@pytest.fixture()
def env(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    srt_path = tmp_path / "9-en.srt"
    srt_path.write_text(SRT, encoding="utf-8")
    with conn:
        conn.execute("INSERT INTO item(rating_key,media_type,title,runtime_ms) "
                     "VALUES('9','movie','Something',6000000)")
        conn.execute(
            "INSERT INTO session(session_id,rating_key,state,claimed,offset_ms,"
            "base_offset_ms,base_at_monotonic,last_seen_at,rebase_threshold_ms,"
            "media_title,duration_ms) "
            "VALUES('s1','9','live',1,600000,600000,1000.0,0,20600,'Something',6000000)")
        conn.execute("INSERT INTO subtitle_cache(rating_key,language,path,cues,fetched_at) "
                     "VALUES('9','en',?,3,1)", (str(srt_path),))
        for claim in (_claim("f1", "live"), _claim("f2", "locked", 3_600_000),
                      _claim("f3", "credits")):
            conn.execute(
                "INSERT INTO claim(rating_key,claim_id,text,source_name,source_url,"
                "reveal_state,unlock_anchor_ms,build_version) VALUES(?,?,?,?,?,?,?,1)",
                ("9", claim["claim_id"], claim["text"], claim["source_name"],
                 claim["source_url"], claim["reveal_state"], claim["unlock_anchor_ms"]))
        conn.execute(
            "INSERT INTO message(id,rating_key,t_ms,kind,text,source_name,build_version,"
            "reveal_state) VALUES('m1','9',580000,'cast','Zoe is in the cast.','plex',1,'live')")
    clock = Clock()

    def build(completer=None, frames=None, transcripts=None):
        return projectionist.AskEngine(
            connect=lambda: conn, settings=Settings(),
            complete=completer or Completer(), frames=frames,
            transcripts=transcripts,
            monotonic=clock), clock, conn

    return build


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_engine_answers_from_claims_and_window(env):
    engine, clock, _ = env()
    ask = engine.start_ask()
    assert ask["state"] == "asking"
    assert wait_for(lambda: (engine.get(ask["ask_id"]) or {}).get("state") == "answered")
    body = engine.get(ask["ask_id"])
    assert body["answer"] == "It is aconite."
    assert body["fact_ids"] == ["f1"]
    assert body["basis"] == "transcript+claims"  # no frame support by default
    prompt = engine._complete.calls[0]["prompt"] if hasattr(engine._complete, "calls") else None
    # The minute-60 claim and the credits claim stay out; the post-playhead
    # cue stays out; the entity-less window keeps the live note in.
    assert "f2 [plex]" not in prompt
    assert "f3 [plex]" not in prompt
    assert "This line is after the playhead." not in prompt
    assert "Zoe is in the cast." in prompt


def test_engine_two_asks_in_one_bucket_make_one_model_call(env):
    engine, clock, _ = env()
    completer = Completer()
    engine._complete = completer
    first = engine.start_ask()
    assert wait_for(lambda: (engine.get(first["ask_id"]) or {}).get("state") == "answered")
    second = engine.start_ask()
    assert wait_for(lambda: (engine.get(second["ask_id"]) or {}).get("state") == "answered")
    assert len(completer.calls) == 1  # cache hit, no second model call


def test_engine_concurrent_asks_share_one_model_call(env):
    engine, clock, _ = env()
    gate = threading.Event()
    completer = Completer(block=gate)
    engine._complete = completer
    first = engine.start_ask()
    time.sleep(0.1)  # let the owner reach the model call
    second = engine.start_ask()  # supersedes the first, same bucket
    gate.set()
    assert wait_for(lambda: (engine.get(second["ask_id"]) or {}).get("state") == "answered")
    assert len(completer.calls) == 1
    first_body = engine.get(first["ask_id"])
    assert first_body["state"] == "superseded"


def test_engine_second_tap_cancels_the_first_in_flight_ask(env):
    engine, clock, _ = env()
    gate = threading.Event()
    completer = Completer(block=gate)
    engine._complete = completer
    first = engine.start_ask()
    time.sleep(0.1)
    clock.advance(120)  # different bucket: the new ask computes for itself
    second = engine.start_ask()
    gate.set()
    assert wait_for(lambda: (engine.get(second["ask_id"]) or {}).get("state") == "answered")
    assert wait_for(lambda: (engine.get(first["ask_id"]) or {}).get("state") == "superseded")
    assert len(completer.calls) == 2


def test_engine_frame_failure_degrades_to_transcript_only(env):
    engine, clock, _ = env()
    completer = Completer()
    engine._complete = completer
    engine._frames = Frames(supported=True, fail=True)
    ask = engine.start_ask()
    assert wait_for(lambda: (engine.get(ask["ask_id"]) or {}).get("state") == "answered")
    assert completer.calls[0]["frame"] is None
    body = engine.get(ask["ask_id"])
    assert body["basis"] == "transcript+claims"


def test_engine_frame_is_passed_to_the_model_when_supported(env):
    engine, clock, _ = env()
    completer = Completer()
    engine._complete = completer
    engine._frames = Frames(supported=True)
    ask = engine.start_ask()
    assert wait_for(lambda: (engine.get(ask["ask_id"]) or {}).get("state") == "answered")
    assert completer.calls[0]["frame"] == b"\xff\xd8fakejpeg"
    assert engine.get(ask["ask_id"])["basis"] == "frame+transcript+claims"


def test_engine_malformed_model_output_is_nothing_not_an_error(env):
    engine, clock, _ = env()
    engine._complete = Completer(answer="garbage{{")
    ask = engine.start_ask()
    assert wait_for(lambda: (engine.get(ask["ask_id"]) or {}).get("state") == "nothing")
    assert engine.get(ask["ask_id"])["reason"] == "unparsable_json"


def test_engine_claim_unlocked_later_appears_once_playhead_passes_it(env):
    engine, clock, _ = env()
    completer = Completer()
    engine._complete = completer
    first = engine.start_ask()
    assert wait_for(lambda: (engine.get(first["ask_id"]) or {}).get("state") == "answered")
    assert "f2 [plex]" not in completer.calls[0]["prompt"]
    clock.advance((3_600_000 - 600_000) / 1000 + 5)
    second = engine.start_ask()
    assert wait_for(lambda: (engine.get(second["ask_id"]) or {}).get("state") == "answered")
    assert "f2 [plex]" in completer.calls[1]["prompt"]


def test_engine_requires_a_claimed_session(env):
    engine, clock, conn = env()
    with conn:
        conn.execute("UPDATE session SET claimed=0")
    with pytest.raises(projectionist.AskUnavailable) as exc:
        engine.start_ask()
    assert exc.value.status == 409


def test_engine_falls_back_to_read_only_transcript_fetch(env):
    # No subtitle cache row for this title (sidecar-built titles predate the
    # cache): the engine asks the injected fetcher instead.
    fetcher = Transcripts(text=SRT)
    engine, clock, conn = env(transcripts=fetcher)
    with conn:
        conn.execute("DELETE FROM subtitle_cache WHERE rating_key='9'")
    completer = Completer()
    engine._complete = completer
    ask = engine.start_ask()
    assert wait_for(lambda: (engine.get(ask["ask_id"]) or {}).get("state") == "answered")
    assert fetcher.fetches == ["9"]
    assert "She picks up the locket." in completer.calls[0]["prompt"]


def test_engine_transcript_fetch_failure_is_not_fatal(env):
    engine, clock, conn = env(transcripts=Transcripts(text=None))
    with conn:
        conn.execute("DELETE FROM subtitle_cache WHERE rating_key='9'")
    completer = Completer()
    engine._complete = completer
    ask = engine.start_ask()
    assert wait_for(lambda: (engine.get(ask["ask_id"]) or {}).get("state") == "answered")
    assert "(no dialogue in the last 30 seconds)" in completer.calls[0]["prompt"]


def test_engine_cites_sources_for_the_answer(env):
    engine, clock, conn = env()
    with conn:
        conn.execute("UPDATE claim SET source_name='wikipedia:article',"
                     "source_url='https://en.wikipedia.org/wiki/Something' "
                     "WHERE claim_id='f1'")
    completer = Completer()
    engine._complete = completer
    ask = engine.start_ask()
    assert wait_for(lambda: (engine.get(ask["ask_id"]) or {}).get("state") == "answered")
    sources = engine.get(ask["ask_id"])["sources"]
    assert sources[0]["source_name"] == "wikipedia:article"
    assert sources[0]["source_url"] == "https://en.wikipedia.org/wiki/Something"


# -- route level --------------------------------------------------------------------

def build_api(tmp_db):
    spec = importlib.util.spec_from_file_location(
        "wwm_api_projectionist", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._conn = store.connect(tmp_db)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/watch-with-me")
    return module, TestClient(app)


class FakeEngine:
    def __init__(self):
        self.asks = {}

    def start_ask(self):
        ask_id = "fake1"
        self.asks[ask_id] = {"ask_id": ask_id, "state": "asking"}
        return dict(self.asks[ask_id])

    def get(self, ask_id):
        body = self.asks.get(ask_id)
        if body and body["state"] == "asking":
            body = dict(body, state="answered", answer="A real answer.",
                        fact_ids=[], confidence=0.9, sources=[])
        return body


@pytest.fixture()
def api(tmp_path, monkeypatch):
    module, client = build_api(tmp_path / "data.db")
    fake = FakeEngine()
    monkeypatch.setattr(module, "_engine", fake)
    return module, client, fake


def test_ask_route_returns_an_ask_id(api):
    module, client, _ = api
    conn = module._conn
    with conn:
        conn.execute(
            "INSERT INTO session(session_id,rating_key,state,claimed,offset_ms,"
            "base_offset_ms,base_at_monotonic,last_seen_at,rebase_threshold_ms) "
            "VALUES('s1','9','live',1,0,0,0,0,20600)")
    response = client.post("/api/plugins/watch-with-me/ask")
    assert response.status_code == 200
    assert response.json()["ask_id"]
    body = client.get("/api/plugins/watch-with-me/ask/fake1").json()
    assert body["status"] == "answered"
    assert body["answer"] == "A real answer."


def test_ask_route_404s_without_a_claimed_session(api):
    _, client, _ = api
    assert client.post("/api/plugins/watch-with-me/ask").status_code == 409


def test_ask_route_404s_when_the_setting_is_off(api):
    module, client, _ = api
    conn = module._conn
    with conn:
        conn.execute(
            "INSERT INTO session(session_id,rating_key,state,claimed,offset_ms,"
            "base_offset_ms,base_at_monotonic,last_seen_at,rebase_threshold_ms) "
            "VALUES('s1','9','live',1,0,0,0,0,20600)")
        conn.execute("INSERT INTO runtime_state(key,value,updated_at) "
                     "VALUES('settings_json',?,1)",
                     (json.dumps({"projectionist": False}),))
    response = client.post("/api/plugins/watch-with-me/ask")
    assert response.status_code == 404
    session = client.get("/api/plugins/watch-with-me/session").json()
    assert session["projectionist"] is False


def test_ask_route_404s_for_an_unknown_ask(api):
    _, client, _ = api
    assert client.get("/api/plugins/watch-with-me/ask/nope").status_code == 404


def test_ask_complete_retries_without_the_frame_when_images_fail(monkeypatch):
    """A note model without vision support degrades to transcript-only."""
    from dashboard import plugin_api  # noqa: PLC0415 - import through the package path
    del plugin_api  # loaded via importlib below instead

    spec = importlib.util.spec_from_file_location(
        "wwm_api_ask_complete", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls = []

    class Llm:
        def complete_structured(self, *, instructions, input, json_mode, task,
                                purpose, **_kw):
            calls.append(input)
            if any(block.get("type") == "image" for block in input):
                raise RuntimeError("model does not accept images")
            return type("R", (), {"text": json.dumps(
                {"answer": "Transcript-only answer.", "fact_ids": [],
                 "confidence": 0.7})})()

    monkeypatch.setattr(projectionist, "_installed_llm", Llm())
    raw = module._ask_complete("prompt", b"\xff\xd8frame", "image/jpeg")
    body = json.loads(raw)
    assert body["answer"] == "Transcript-only answer."
    assert len(calls) == 2  # image attempt, then the transcript-only retry
    assert all(block.get("type") == "text" for block in calls[1])
