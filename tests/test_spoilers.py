"""Spoiler escrow: release states, character quarantine, prompt gating.

These tests assert on the built prompt text (never on the model) and on the
withheld set that becomes the end-of-session credits artifact.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from watch_with_me import build, cues, projectionist, spoilers, store
from watch_with_me.transcript import parse_srt


def line(minute: int, text: str, second: int = 0) -> dict:
    return {"start_ms": (minute * 60 + second) * 1000,
            "end_ms": (minute * 60 + second) * 1000 + 2000,
            "text": text}


def segment(end_minute: int, lines: list[dict]) -> dict:
    return {"start_ms": 0, "end_ms": end_minute * 60_000, "lines": lines}


EARLY_LINES = [line(10, "We should never have come back here."),
               line(10, "The storm is getting worse.")]


def test_character_named_at_minute_sixty_is_absent_from_the_minute_ten_prompt():
    claims = [{"id": "f1", "text": "Zoe's locket holds the key to the bunker.",
               "source": "test-source", "url": "https://example.test/zoe"}]
    transcript = EARLY_LINES + [line(60, "Zoe, don't open that locket.")]
    classified, withheld = spoilers.classify_claims(claims, transcript)

    early_prompt = cues.build_prompt(segment(10, EARLY_LINES), classified, "transcript")
    assert "Zoe" not in early_prompt
    assert "locket" not in early_prompt

    late_prompt = cues.build_prompt(segment(61, transcript), classified, "transcript")
    assert "locket" in late_prompt


def test_locked_claim_appears_in_the_credits_artifact_with_its_source():
    claims = [{"id": "f1", "text": "Zoe's locket holds the key to the bunker.",
               "source": "test-source", "url": "https://example.test/zoe"}]
    transcript = EARLY_LINES + [line(60, "Zoe, don't open that locket.")]
    _, withheld = spoilers.classify_claims(claims, transcript)

    assert len(withheld) == 1
    entry = withheld[0]
    assert entry["reveal_state"] == "locked"
    assert entry["unlock_anchor_ms"] == 60 * 60_000
    assert entry["source_name"] == "test-source"
    assert entry["source_url"] == "https://example.test/zoe"


def test_claim_whose_subject_never_appears_is_credits_never_live():
    claims = [{"id": "f1", "text": "The lighthouse keeper is the killer.",
               "source": "test-source", "url": None}]
    classified, withheld = spoilers.classify_claims(claims, EARLY_LINES)

    assert [c["reveal_state"] for c in classified] == []  # nothing visible
    assert len(withheld) == 1
    assert withheld[0]["reveal_state"] == "credits"
    assert withheld[0]["unlock_anchor_ms"] is None
    # No segment, however late, may ever show it.
    late = segment(120, EARLY_LINES)
    assert "lighthouse" not in cues.build_prompt(late, classified, "transcript")


def test_cast_claim_with_unspoken_character_is_credits_actor_name_is_live():
    claims = [{"id": "f1", "text": "Liam Neeson plays Ra's al Ghul.",
               "source": "plex", "url": None}]
    classified, withheld = spoilers.classify_claims(claims, EARLY_LINES)

    # The role half never appears in any prompt...
    prompt = cues.build_prompt(segment(10, EARLY_LINES), classified, "transcript")
    assert "Ra's al Ghul" not in prompt
    assert "plays" not in prompt
    # ...the claim sits in the credits set...
    assert [w["reveal_state"] for w in withheld] == ["credits"]
    # ...and a companion claim names only the actor, live from minute one.
    actor_claims = [c for c in classified if "Liam Neeson" in c["text"]]
    assert len(actor_claims) == 1
    assert actor_claims[0]["reveal_state"] == "live"
    assert actor_claims[0]["unlock_anchor_ms"] is None
    assert "Liam Neeson" in prompt


def test_cast_claim_with_spoken_character_is_locked_until_the_name_is_spoken():
    claims = [{"id": "f1", "text": "Veronica Ferres plays Dr. Keyes.",
               "source": "plex", "url": None}]
    transcript = EARLY_LINES + [line(30, "Dr. Keyes, the core is unstable.")]
    classified, withheld = spoilers.classify_claims(claims, transcript)

    claim = next(c for c in classified if c["id"] == "f1")
    assert claim["reveal_state"] == "locked"
    assert claim["unlock_anchor_ms"] == 30 * 60_000
    assert claim["subject_key"] == "dr keyes"

    early = cues.build_prompt(segment(10, EARLY_LINES), classified, "transcript")
    assert "Dr. Keyes" not in early
    late = cues.build_prompt(segment(31, transcript), classified, "transcript")
    assert "Dr. Keyes" in late
    # The locked claim is also listed in the artifact with its unlock time.
    assert [(w["reveal_state"], w["unlock_anchor_ms"]) for w in withheld] \
        == [("locked", 30 * 60_000)]


def test_plex_metadata_and_wikipedia_claims_are_live_from_minute_one():
    claims = [
        {"id": "f1", "text": "Directed by Jordan Downey.", "source": "plex", "url": None},
        {"id": "f2", "text": "Studio: BoulderLight Pictures.", "source": "plex", "url": None},
        {"id": "f3", "text": "Shot on location in Canada.", "source": "wikipedia:production",
         "url": "https://en.wikipedia.org/wiki/X"},
        {"id": "f4", "text": "Someone is known for their stage work.",
         "source": "wikipedia", "url": None},
    ]
    classified, withheld = spoilers.classify_claims(claims, EARLY_LINES)
    assert all(c["reveal_state"] == "live" for c in classified)
    assert withheld == []
    prompt = cues.build_prompt(segment(10, EARLY_LINES), classified, "transcript")
    for snippet in ("Jordan Downey", "BoulderLight", "Canada", "stage work"):
        assert snippet in prompt


def test_model_citing_a_locked_fact_id_is_rejected():
    # Fact ids are guessable (f1, f2, ...), so parse_response must gate
    # citations against release state, not just membership.
    facts = [{"id": "f1", "text": "Veronica Ferres plays Dr. Keyes.", "source": "plex",
              "url": None, "reveal_state": "locked", "unlock_anchor_ms": 30 * 60_000,
              "subject_key": "dr keyes"}]
    seg = segment(10, EARLY_LINES)
    raw = json.dumps({"messages": [{
        "anchor": "We should never have come back here.", "kind": "cast",
        "text": "Dr. Keyes is secretly the villain.", "fact_ids": ["f1"],
        "confidence": 0.99,
    }]})
    messages, stats = cues.parse_response(raw, facts, seg)
    assert messages == []
    assert stats["reasons"].get("withheld_fact_cited") == 1


def test_unclassified_sheets_keep_old_behaviour():
    # Hand-built sheets without classification fields stay fully visible.
    facts = [{"id": "f1", "text": "Anything at all.", "source": "manual", "url": None}]
    prompt = cues.build_prompt(segment(10, EARLY_LINES), facts, "transcript")
    assert "Anything at all." in prompt


def test_persist_round_trips_withheld_claims_to_the_claim_table(tmp_path):
    conn = store.connect(tmp_path / "data.db")
    claims = [{"id": "f1", "text": "Zoe's locket holds the key to the bunker.",
               "source": "test-source", "url": "https://example.test/zoe"}]
    transcript = EARLY_LINES + [line(60, "Zoe, don't open that locket.")]
    _, withheld = spoilers.classify_claims(claims, transcript)
    store.persist_claims(conn, "42", withheld)

    artifact = store.withheld_claims(conn, "42")
    assert len(artifact) == 1
    assert artifact[0]["text"] == claims[0]["text"]
    assert artifact[0]["reveal_state"] == "locked"
    assert artifact[0]["source_name"] == "test-source"
    assert artifact[0]["unlock_anchor_ms"] == 60 * 60_000
    # A different title has no artifact.
    assert store.withheld_claims(conn, "99") == []
    conn.close()


def test_build_end_to_end_withheld_claims_reach_the_store(tmp_path):
    metadata = {"ratingKey": "42", "type": "movie", "title": "Test Feature",
                "year": 2026, "duration": 7_200_000, "Chapter": [],
                "Role": [{"tag": "Harrison Ford", "role": "Han Solo"}]}
    srt = ("1\n00:00:10,000 --> 00:00:12,000\nHe was a good friend.\n\n"
           "2\n00:01:00,000 --> 00:01:02,000\nThe shield generator is down.\n\n"
           "3\n00:10:00,000 --> 00:10:02,000\nHan Solo is gone.\n")

    def complete(prompt: str) -> str:
        return json.dumps({"messages": [{
            "anchor": "He was a good friend.", "kind": "cast",
            "text": "Released in 2026.", "fact_ids": ["f2"], "confidence": 0.9,
        }]})

    result = build.build_messages(metadata, srt, [], complete)
    # Han Solo is first spoken at 10:00, so the cast claim is locked until
    # then; the sourced note citing f2 streams immediately.
    assert len(result.messages) == 1
    assert result.messages[0]["reveal_state"] == "live"
    assert any("Han Solo" in e["text"] for e in result.withheld)

    conn = store.connect(tmp_path / "data.db")
    build.persist(conn, result, metadata)
    artifact = store.withheld_claims(conn, "42")
    assert any("Han Solo" in entry["text"] for entry in artifact)

    # The runtime query the Projectionist asks: only claims unlocked at the
    # playhead. Minute one excludes the locked cast claim entirely (actor
    # naming rides along only in the credits-only case, per the design).
    early = store.claims_unlocked_at(conn, "42", 60_000)
    early_texts = " ".join(c["text"] for c in early)
    assert "Han Solo" not in early_texts
    assert "Harrison Ford" not in early_texts
    assert "Released in 2026." in early_texts
    late = store.claims_unlocked_at(conn, "42", 601_000)
    assert any("Han Solo" in c["text"] for c in late)
    conn.close()


# -- entity-expansion notes: the same classifier's second kind of claim --------

def _entity_note(**overrides) -> dict:
    note = {"id": "msg_ctx1", "t_ms": 30 * 60_000, "kind": "context",
            "text": "Aconitum is a genus of over 250 species of flowering plants.",
            "anchor": "That is aconitum on the table.",
            "anchor_ratio": 1.0, "fact_ids": [], "model_only": 0,
            "source_name": "wikipedia:Aconitum",
            "source_url": "https://en.wikipedia.org/wiki/Aconitum",
            "confidence": 0.9, "subject": "aconitum"}
    note.update(overrides)
    return note


def test_entity_note_is_live_with_its_mention_line_as_unlock_anchor():
    transcript = EARLY_LINES + [line(30, "That is aconitum on the table.")]
    note = spoilers.classify_entity_note(_entity_note(), transcript,
                                         title="Test Feature")

    assert note["reveal_state"] == "live"
    assert note["unlock_anchor_ms"] == 30 * 60_000
    assert note["subject_key"] == "aconitum"
    # The input dict is not mutated in place.
    assert "reveal_state" not in _entity_note()


def test_entity_note_mentions_earlier_than_the_note_anchor_to_the_first_line():
    # The entity was spoken once before the segment whose Pass B run wrote
    # the note: the anchor is the FIRST naming line, not the note's own time.
    transcript = EARLY_LINES + [line(5, "She mentions aconitum in passing.")] \
        + [line(30, "That is aconitum on the table.")]
    note = spoilers.classify_entity_note(_entity_note(), transcript)

    assert note["reveal_state"] == "live"
    assert note["unlock_anchor_ms"] == 5 * 60_000


def test_entity_note_named_later_than_the_note_is_locked():
    transcript = EARLY_LINES + [line(60, "That is aconitum on the table.")]
    note = spoilers.classify_entity_note(_entity_note(), transcript)

    assert note["reveal_state"] == "locked"
    assert note["unlock_anchor_ms"] == 60 * 60_000


def test_entity_note_that_claims_about_the_title_is_credits():
    by_title = spoilers.classify_entity_note(
        _entity_note(text="Test Feature was inspired by Aconitum."),
        EARLY_LINES, title="Test Feature")
    assert by_title["reveal_state"] == "credits"
    assert by_title["unlock_anchor_ms"] is None

    # The phrase heuristic also fires without a title to compare against —
    # the entity pass is instructed never to reference the work itself.
    by_phrase = spoilers.classify_entity_note(
        _entity_note(text="Aconitum is a nod to the film's poison motif."),
        EARLY_LINES)
    assert by_phrase["reveal_state"] == "credits"


def test_entity_note_is_absent_from_an_ask_prompt_before_its_mention():
    # Entity notes never enter segment prompts (build_prompt gates the fact
    # sheet only); the ask prompt is the first prompt they can appear in.
    transcript = EARLY_LINES + [line(30, "That is aconitum on the table.")]
    note = spoilers.classify_entity_note(_entity_note(), transcript,
                                         title="Test Feature")
    assert note["reveal_state"] == "live"

    before = projectionist.notes_for_ask([note], 10 * 60_000,
                                         "the storm is getting worse")
    assert before == []
    after = projectionist.notes_for_ask([note], 31 * 60_000,
                                        "look at that aconitum on the table")
    assert after == [note]


class _StubExpander:
    """Returns a canned context note once, then []; mimics entities.Expander's
    build-wide expanded-name tracking."""

    def __init__(self, notes: list[dict]) -> None:
        self.notes = notes
        self.stats = {"calls": 0, "fetches": 0, "notes": len(notes),
                      "skipped_names": 0, "unusable": 0}

    def expand(self, segment, rendered, pass_a_count) -> list[dict]:
        notes, self.notes = self.notes, []
        return notes


_ENTITY_SRT = ("1\n00:00:10,000 --> 00:00:12,000\nHe was a good friend.\n\n"
               "2\n00:04:00,000 --> 00:04:02,000\nUganda, of all places.\n\n")
_ENTITY_METADATA = {"ratingKey": "42", "type": "movie",
                    "title": "Test Feature", "year": 2026,
                    "duration": 7_200_000, "Chapter": []}


def _pass_a_completer() -> object:
    def complete(prompt: str) -> str:
        return json.dumps({"messages": [{
            "anchor": "He was a good friend.", "kind": "context",
            "text": "The storm is getting worse.", "fact_ids": [],
            "confidence": 0.9,
        }]})
    return complete


def test_build_classifies_entity_notes_and_persists_the_release_state(tmp_path):
    # The mention sits at 4:00, past apply_density's 180s window from the
    # Pass A note, so both notes survive to the sourced filter.
    note = _entity_note(id="msg_ctxug", t_ms=240_000,
                        text="Uganda is a landlocked country in East Africa.",
                        anchor="Uganda, of all places.", subject="uganda",
                        source_name="wikipedia:Uganda")
    expander = _StubExpander([note])
    result = build.build_messages(_ENTITY_METADATA, _ENTITY_SRT, [],
                                  _pass_a_completer(), expander=expander)

    context = next(m for m in result.messages if m["kind"] == "context"
                   and m.get("source_name") == "wikipedia:Uganda")
    assert context["reveal_state"] == "live"
    assert context["unlock_anchor_ms"] == 240_000  # the mention line
    assert context["subject_key"] == "uganda"
    assert result.stats["entity_withheld"] == 0

    conn = store.connect(tmp_path / "data.db")
    build.persist(conn, result, _ENTITY_METADATA)
    timeline = store.timeline_for(conn, "42")
    assert len(timeline) == len(result.messages)
    # No note reaches the store — or the page — with a null release state.
    assert all(row["reveal_state"] in spoilers.REVEAL_STATES for row in timeline)
    stored = next(r for r in timeline if r["source_name"] == "wikipedia:Uganda")
    assert stored["unlock_anchor_ms"] == 240_000
    conn.close()


def test_build_withholds_a_title_claiming_entity_note_from_the_stream(tmp_path):
    # The prompt-bug case: the note claims something about the title itself.
    bad = _entity_note(id="msg_ctxbad", t_ms=240_000,
                       text="Test Feature was shot on location in Uganda.",
                       anchor="Uganda, of all places.", subject="uganda",
                       source_name="wikipedia:Uganda")
    result = build.build_messages(_ENTITY_METADATA, _ENTITY_SRT, [],
                                  _pass_a_completer(),
                                  expander=_StubExpander([bad]))

    assert all(m.get("source_name") != "wikipedia:Uganda" for m in result.messages)
    entry = next(e for e in result.withheld
                 if e["claim_id"] == "msg_ctxbad")
    assert entry["reveal_state"] == "credits"
    assert entry["model_only"] == 0  # grounded, not part of the model-only cap
    assert result.stats["entity_withheld"] == 1


def test_phase7_cycle_replay_has_zero_null_release_states():
    # The phase8 gate, offline: replay the phase7 build artifact's entity
    # notes through the new classifier and assert the invariant the page now
    # relies on. (The full rebuild lands in artifacts/phase8/rebuild.py.)
    root = Path(__file__).resolve().parent.parent
    phase7 = root / "artifacts" / "phase7"
    if not (phase7 / "cycle.json").exists() or not (phase7 / "cycle.srt").exists():
        pytest.skip("phase7 artifacts are not present in this checkout")
    artifact = json.loads((phase7 / "cycle.json").read_text("utf-8"))
    transcript = parse_srt((phase7 / "cycle.srt").read_text("utf-8",
                                                            errors="replace"))

    classified = [spoilers.classify_entity_note(n, transcript, title="The Cycle")
                  if n.get("reveal_state") is None else n
                  for n in artifact["messages"]]

    distribution = Counter(n["reveal_state"] for n in classified)
    assert distribution.get(None, 0) == 0
    # Every entity note from the real build is safe by construction: live at
    # its mention line. Any credits/locked entry here is a prompt bug to
    # report, and the assertion fails loudly if one appears.
    for source in ("wikipedia:Uganda", "wikipedia:Sovereign citizen movement",
                   "wikipedia:Aconitum"):
        note = next(n for n in classified if n.get("source_name") == source)
        assert note["reveal_state"] == "live", note["text"]
        assert note["unlock_anchor_ms"] is not None
        assert note["unlock_anchor_ms"] <= note["t_ms"]
    assert not any(n["reveal_state"] == "credits" and n["kind"] == "context"
                   for n in classified)
