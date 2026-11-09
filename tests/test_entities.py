"""Entity-expansion (Pass B): parsing, caching, rejection rules, settings flag."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_title  # noqa: E402
from watch_with_me import cues, entities  # noqa: E402
from watch_with_me.config import Settings  # noqa: E402

LINES = [
    {"start_ms": 1000, "end_ms": 3000,
     "text": "They drove all the way to New Mexico that night."},
    {"start_ms": 5000, "end_ms": 7000,
     "text": "The desert went on for miles."},
]

SEGMENT = {"start_ms": 0, "end_ms": 10_000, "lines": LINES}

SUMMARY = ("New Mexico is a state in the Southwestern region of the United States. "
           "It is one of the Mountain States of the southern Rocky Mountains. "
           "Its capital is Santa Fe.")

ANCHOR = "They drove all the way to New Mexico that night."


def entities_payload(*names: str) -> str:
    return json.dumps({"entities": [{"name": n, "why": "mentioned in dialogue"}
                                    for n in names]})


def note_payload(text: str = "New Mexico is the fifth-largest state in the US.",
                 anchor: str = ANCHOR) -> str:
    return json.dumps({"messages": [{"anchor": anchor, "text": text,
                                     "confidence": 0.9}]})


class TwoPassCompleter:
    """Serves the extraction call then the note call, recording prompts."""

    def __init__(self, names=("New Mexico",), note_text="unused"):
        self.names = names
        # "unused" -> return a real note; None -> empty messages (no note)
        self.note_text = ("New Mexico is the fifth-largest state in the US."
                          if note_text == "unused" else note_text)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "You identify real-world entities" in prompt:
            return entities_payload(*self.names)
        if "You write one short trivia note" in prompt:
            if self.note_text is None:
                return json.dumps({"messages": []})
            return note_payload(self.note_text)
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")


# --- entity JSON parsing -----------------------------------------------------

def test_parse_entities_accepts_clean_payload():
    out = entities.parse_entities(entities_payload("New Mexico", "Trinity"))
    assert [e["name"] for e in out] == ["New Mexico", "Trinity"]
    assert all(e["why"] for e in out)


def test_parse_entities_rejects_garbage():
    assert entities.parse_entities("not json") == []
    assert entities.parse_entities('{"entities": "New Mexico"}') == []
    assert entities.parse_entities('{"entities": [42, {"why": "x"}]}') == []


def test_parse_entities_drops_generic_names_and_pronouns():
    raw = entities_payload("mom", "car", "he", "New Mexico")
    out = entities.parse_entities(raw)
    assert [e["name"] for e in out] == ["New Mexico"]


def test_parse_entities_keeps_lowercase_multiword_titles():
    # "mojo bag" is all lowercase but a real article (stylized title): the
    # fetch decides, not the name shape.
    out = entities.parse_entities(entities_payload("mojo bag"))
    assert [e["name"] for e in out] == ["mojo bag"]


def test_parse_entities_caps_at_two_and_dedupes():
    out = entities.parse_entities(
        entities_payload("New Mexico", "new mexico", "Trinity", "Roswell"))
    assert [e["name"] for e in out] == ["New Mexico", "Trinity"]


# --- stub / disambiguation rejection ------------------------------------------

def test_usable_summary_rejects_stubs_and_disambiguation():
    assert not entities.usable_summary("Short page.")
    assert not entities.usable_summary("Santa may refer to: Santa Claus, Santa Maria.")
    assert entities.usable_summary(SUMMARY)


def _fake_urlopen(extract: str, missing: bool = False):
    page = {"missing": ""} if missing else {"title": "New Mexico", "extract": extract}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"query": {"pages": {"1": page}}}).encode()

    def fake(request, timeout=None):
        return Response()

    return fake


def test_fetch_summary_rejects_disambiguation(monkeypatch):
    monkeypatch.setattr(entities.urllib.request, "urlopen",
                        _fake_urlopen("Santa may refer to: several places."))
    assert entities.fetch_summary("Santa") == {"missing": True}


def test_fetch_summary_rejects_missing_page(monkeypatch):
    monkeypatch.setattr(entities.urllib.request, "urlopen",
                        _fake_urlopen("", missing=True))
    assert entities.fetch_summary("Nobody Famous") == {"missing": True}


def test_fetch_summary_keeps_head_and_url(monkeypatch):
    monkeypatch.setattr(entities.urllib.request, "urlopen", _fake_urlopen(SUMMARY))
    entry = entities.fetch_summary("New Mexico")
    assert entry["url"] == "https://en.wikipedia.org/wiki/New_Mexico"
    assert entry["summary"].startswith("New Mexico is a state")
    assert "Santa Fe" in entry["summary"]  # third sentence kept


# --- cache --------------------------------------------------------------------

def test_same_entity_in_two_segments_fetches_once(tmp_path, monkeypatch):
    fetches: list[str] = []

    def fake_fetch(name, **kwargs):
        fetches.append(name)
        return {"name": name, "url": "https://en.wikipedia.org/wiki/New_Mexico",
                "summary": SUMMARY}

    monkeypatch.setattr(entities, "fetch_summary", fake_fetch)
    expander = entities.Expander(TwoPassCompleter(),
                                 cache=entities.EntityCache(tmp_path / "c.json"))
    first = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    second = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    assert len(first) == 1 and first[0]["kind"] == "context"
    assert second == []  # already expanded this build
    assert fetches == ["New Mexico"]  # exactly one fetch for both segments


def test_disk_cache_survives_across_expanders(tmp_path, monkeypatch):
    fetches: list[str] = []

    def fake_fetch(name, **kwargs):
        fetches.append(name)
        return {"name": name, "url": "https://en.wikipedia.org/wiki/New_Mexico",
                "summary": SUMMARY, "extract": SUMMARY}

    monkeypatch.setattr(entities, "fetch_summary", fake_fetch)
    cache_path = tmp_path / "c.json"
    entities.Expander(TwoPassCompleter(),
                      cache=entities.EntityCache(cache_path)).expand(
                          SEGMENT, "t", pass_a_count=0)
    entities.Expander(TwoPassCompleter(),
                      cache=entities.EntityCache(cache_path)).expand(
                          SEGMENT, "t", pass_a_count=0)
    assert fetches == ["New Mexico"]


def test_note_cap_is_per_segment_not_per_build(tmp_path, monkeypatch):
    """A note in one segment must not silence later segments (regression)."""
    monkeypatch.setattr(
        entities, "fetch_summary",
        lambda name, **kw: {"name": name, "url": "https://en.wikipedia.org/wiki/X",
                            "summary": SUMMARY})

    class ShiftingCompleter:
        """Each extraction call names a fresh entity so notes can repeat."""

        def __init__(self):
            self.count = 0

        def __call__(self, prompt: str) -> str:
            if "You identify real-world entities" in prompt:
                self.count += 1
                return json.dumps({"entities": [
                    {"name": f"Entity {self.count}", "why": "mentioned"}]})
            if "You write one short trivia note" in prompt:
                return note_payload(f"Entity {self.count} is a real thing.")
            raise AssertionError(prompt[:80])

    expander = entities.Expander(ShiftingCompleter(),
                                 cache=entities.EntityCache(tmp_path / "c.json"))
    first = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    second = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    assert len(first) == 1 and len(second) == 1
    assert first[0]["text"] != second[0]["text"]
    assert expander.stats["notes"] == 2


def test_negative_results_are_cached_too(tmp_path, monkeypatch):
    fetches: list[str] = []

    def fake_fetch(name, **kwargs):
        fetches.append(name)
        return {"missing": True}

    monkeypatch.setattr(entities, "fetch_summary", fake_fetch)
    cache_path = tmp_path / "c.json"
    for _ in range(2):
        expander = entities.Expander(TwoPassCompleter(),
                                     cache=entities.EntityCache(cache_path))
        assert expander.expand(SEGMENT, "t", pass_a_count=0) == []
    assert len(fetches) == 1  # the negative result is cached on disk too


def test_default_cache_lives_under_store_data_dir(tmp_path, monkeypatch):
    from watch_with_me import store

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delitem(sys.modules, "plugins.plugin_storage", raising=False)
    cache = entities.EntityCache()
    assert cache.path.parent == store.data_dir()
    assert cache.path.name == "entity-cache.json"


# --- prompt rules --------------------------------------------------------------

def test_no_connection_claim_rule_is_in_the_note_prompt():
    assert "must not claim the film references" in entities.NOTE_RULES
    assert "inspired by, or is connected to" in entities.NOTE_RULES
    completer = TwoPassCompleter()
    expander = entities.Expander(completer, cache=entities.EntityCache())
    entry = {"name": "New Mexico", "url": "https://en.wikipedia.org/wiki/New_Mexico",
             "summary": SUMMARY}
    expander._write_note(SEGMENT, "transcript",
                         {"name": "New Mexico", "why": "setting"}, entry)
    note_prompt = completer.prompts[-1]
    assert "must not claim the film references" in note_prompt
    assert "WIKIPEDIA FACTS" in note_prompt


def test_context_note_is_grounded_and_resolves_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr(
        entities, "fetch_summary",
        lambda name, **kw: {"name": name,
                            "url": "https://en.wikipedia.org/wiki/New_Mexico",
                            "summary": SUMMARY})
    expander = entities.Expander(TwoPassCompleter(),
                                 cache=entities.EntityCache(tmp_path / "c.json"))
    notes = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    assert len(notes) == 1, expander.stats
    note = notes[0]
    assert note["model_only"] == 0  # does not consume the model-only budget
    assert note["source_name"] == "wikipedia:New Mexico"
    assert note["source_url"] == "https://en.wikipedia.org/wiki/New_Mexico"
    assert note["t_ms"] == 1000


# --- caps -----------------------------------------------------------------------

def test_segment_with_three_pass_a_notes_skips_pass_b(tmp_path, monkeypatch):
    monkeypatch.setattr(
        entities, "fetch_summary",
        lambda name, **kw: {"missing": True})
    completer = TwoPassCompleter()
    expander = entities.Expander(completer,
                                 cache=entities.EntityCache(tmp_path / "c.json"))
    assert expander.expand(SEGMENT, "transcript", pass_a_count=3) == []
    assert completer.prompts == []


def test_at_most_one_note_and_two_fetches_per_segment(tmp_path, monkeypatch):
    fetches: list[str] = []

    def fake_fetch(name, **kwargs):
        fetches.append(name)
        return {"name": name, "url": f"https://en.wikipedia.org/wiki/{name}",
                "summary": SUMMARY}

    monkeypatch.setattr(entities, "fetch_summary", fake_fetch)
    # No note is ever written (note_text=None), so the loop keeps going and
    # the fetch cap — not the note cap — is what stops it.
    completer = TwoPassCompleter(names=("New Mexico", "Trinity", "Roswell"),
                                 note_text=None)
    expander = entities.Expander(completer,
                                 cache=entities.EntityCache(tmp_path / "c.json"))
    notes = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    assert notes == []
    assert fetches == ["New Mexico", "Trinity"]  # third entity never fetched


def test_excluded_cast_names_are_dropped_in_code(tmp_path, monkeypatch):
    fetches: list[str] = []

    def fake_fetch(name, **kwargs):
        fetches.append(name)
        return {"missing": True}

    monkeypatch.setattr(entities, "fetch_summary", fake_fetch)
    completer = TwoPassCompleter(names=("Jack O'Connell",))
    expander = entities.Expander(
        completer, cache=entities.EntityCache(tmp_path / "c.json"),
        excluded=entities.cast_names({
            "Role": [{"tag": "Jack O'Connell", "role": "Eric"}]}))
    assert expander.expand(SEGMENT, "transcript", pass_a_count=0) == []
    assert fetches == []
    assert expander.stats["skipped_names"] == 1


# --- settings flag / byte-identical Pass A -------------------------------------

def test_settings_flag_gates_the_expander():
    metadata = {"Role": [{"tag": "Jack O'Connell", "role": "Eric"}]}
    on = build_title.build_expander(Settings(entity_expansion=True),
                                    lambda p: "", metadata, wikipedia=True)
    assert isinstance(on, entities.Expander)
    assert build_title.build_expander(Settings(entity_expansion=False),
                                      lambda p: "", metadata,
                                      wikipedia=True) is None
    assert build_title.build_expander(Settings(entity_expansion=True),
                                      lambda p: "", metadata,
                                      wikipedia=False) is None


def test_flag_off_leaves_pass_a_output_identical():
    facts = [{"id": "f1", "text": "Shot on location in New Mexico.",
              "source": "wikipedia", "url": "https://en.wikipedia.org/wiki/X"}]
    calls = {"n": 0}

    def completer(prompt: str) -> str:
        calls["n"] += 1
        return json.dumps({"messages": [{
            "anchor": ANCHOR, "kind": "production",
            "text": "The crew shot these scenes near Albuquerque.",
            "fact_ids": ["f1"], "confidence": 0.9}]})

    segments = [dict(SEGMENT, lines=[dict(l) for l in LINES])]
    expander = build_title.build_expander(Settings(entity_expansion=False),
                                          completer, {}, wikipedia=True)
    without = cues.generate(segments, facts, completer, expander=expander)
    expander = build_title.build_expander(Settings(entity_expansion=False),
                                          completer, {}, wikipedia=False)
    assert expander is None
    again = cues.generate(segments, facts, completer, expander=expander)
    assert without == again
    assert calls["n"] == 2  # one model call per segment, no Pass B calls


def test_generate_merges_entity_stats_into_totals(tmp_path, monkeypatch):
    monkeypatch.setattr(
        entities, "fetch_summary",
        lambda name, **kw: {"name": name, "url": "https://en.wikipedia.org/wiki/X",
                            "summary": SUMMARY})

    def completer(prompt: str) -> str:
        if "You identify real-world entities" in prompt:
            return entities_payload("New Mexico")
        if "You write one short trivia note" in prompt:
            return note_payload()
        return json.dumps({"messages": []})

    segments = [dict(SEGMENT, lines=[dict(l) for l in LINES])]
    expander = entities.Expander(completer,
                                 cache=entities.EntityCache(tmp_path / "c.json"))
    messages, totals, withheld = cues.generate(segments, [], completer, expander=expander)
    assert totals["entity"]["notes"] == 1
    assert totals["entity"]["fetches"] == 1
    assert totals["after_sourced_filter"] == 1
    assert len(withheld) == 0  # no claims in the sheet; the cap allows no notes
    assert messages[0]["model_only"] == 0


# --- rabbit hole: place notes and the two-note segment budget ------------------

PLACE_EXTRACT = (
    "New Mexico is a state in the Southwestern United States. "
    "Its capital is Santa Fe. "
    "New Mexico has the highest percentage of Hispanic Americans of any state. "
    "New Mexico has more PhDs per capita than any other state."
)


class PlaceCompleter:
    """Extraction call only: a place note must not need a note-writing call."""

    def __init__(self, *names):
        self.names = names
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "You identify real-world entities" in prompt:
            return entities_payload(*self.names)
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")


def _place_entry(**overrides):
    entry = {"name": "New Mexico",
             "url": "https://en.wikipedia.org/wiki/New_Mexico",
             "summary": SUMMARY, "extract": PLACE_EXTRACT, "place": True,
             "place_fact": "New Mexico has the highest percentage of "
                           "Hispanic Americans of any state."}
    entry.update(overrides)
    return entry


def test_place_article_gets_a_deterministic_note(tmp_path, monkeypatch):
    monkeypatch.setattr(entities, "fetch_summary",
                        lambda name, **kw: _place_entry())
    completer = PlaceCompleter("New Mexico")
    expander = entities.Expander(completer,
                                 cache=entities.EntityCache(tmp_path / "c.json"))
    notes = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    assert len(notes) == 1
    note = notes[0]
    assert note["text"] == _place_entry()["place_fact"]  # verbatim, no rewrite
    assert note["t_ms"] == 1000  # anchored at the line that mentions it
    assert note["model_only"] == 0
    assert note["source_name"] == "wikipedia:New Mexico"
    # The note-writing model call never happened.
    assert len(completer.prompts) == 1
    assert expander.stats["calls"] == 1


def test_place_without_a_place_fact_falls_back_to_model_note(tmp_path, monkeypatch):
    monkeypatch.setattr(entities, "fetch_summary",
                        lambda name, **kw: _place_entry(place_fact=None))
    completer = TwoPassCompleter()
    expander = entities.Expander(completer,
                                 cache=entities.EntityCache(tmp_path / "c.json"))
    notes = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    assert len(notes) == 1
    assert "You write one short trivia note" in completer.prompts[-1]


def test_two_distinct_entities_give_two_notes_per_segment(tmp_path, monkeypatch):
    entries = {"New Mexico": _place_entry(), "Trinity": {
        "name": "Trinity", "url": "https://en.wikipedia.org/wiki/Trinity",
        "summary": "Trinity was the first test of a nuclear weapon.",
        "extract": "Trinity was the first test of a nuclear weapon, conducted "
                   "in July 1945 in New Mexico.",
        "place": False, "place_fact": None}}
    monkeypatch.setattr(entities, "fetch_summary",
                        lambda name, **kw: entries[name])
    completer = TwoPassCompleter(names=("New Mexico", "Trinity"))
    expander = entities.Expander(completer,
                                 cache=entities.EntityCache(tmp_path / "c.json"))
    notes = expander.expand(SEGMENT, "transcript", pass_a_count=0)
    assert len(notes) == 2
    assert {n["subject"] for n in notes} == {"new mexico", "trinity"}
