from __future__ import annotations

import json

from watch_with_me import cues

LINES = [
    {"start_ms": 1000, "end_ms": 3000, "text": "He was a good friend."},
    {"start_ms": 5000, "end_ms": 7000, "text": "The shield generator is down."},
]


def facts():
    return [{"id": "f1", "text": "Harrison Ford plays Han Solo.", "source": "plex", "url": None},
            {"id": "f2", "text": "Shot on location in Tunisia.", "source": "wikipedia",
             "url": "https://en.wikipedia.org/wiki/X"}]


def segment():
    return {"start_ms": 0, "end_ms": 10_000, "lines": LINES}


def response(payload):
    return json.dumps(payload)


def test_anchor_resolves_to_the_line_start():
    hit = cues.resolve_anchor("he was a good friend", LINES)
    assert hit == {"t_ms": 1000, "ratio": 1.0}


def test_anchor_miss_returns_none():
    assert cues.resolve_anchor("a line that is not in the transcript at all", LINES) is None


def test_valid_message_keeps_its_fact_and_time():
    raw = response({"messages": [{
        "anchor": "He was a good friend.", "kind": "cast",
        "text": "That is Harrison Ford.", "fact_ids": ["f1"], "confidence": 0.9,
    }]})
    messages, stats = cues.parse_response(raw, facts(), segment())
    assert stats["returned"] == 1
    assert messages[0]["t_ms"] == 1000
    assert messages[0]["source_name"] == "plex"
    assert messages[0]["model_only"] == 0


def test_unknown_fact_ids_become_model_only():
    raw = response({"messages": [{
        "anchor": "The shield generator is down.", "kind": "craft",
        "text": "That model was reused later.", "fact_ids": ["nope"], "confidence": 0.9,
    }]})
    messages, _ = cues.parse_response(raw, facts(), segment())
    assert messages[0]["model_only"] == 1
    assert messages[0]["source_url"] is None


def test_bad_kind_long_text_and_bad_anchor_are_rejected():
    raw = response({"messages": [
        {"anchor": "He was a good friend.", "kind": "opinion", "text": "x", "confidence": 0.9},
        {"anchor": "He was a good friend.", "kind": "cast", "text": "y" * 300, "confidence": 0.9},
        {"anchor": "not in the segment", "kind": "cast", "text": "z", "confidence": 0.9},
        {"anchor": "He was a good friend.", "kind": "cast", "text": "low conf", "confidence": 0.2},
    ]})
    messages, stats = cues.parse_response(raw, facts(), segment())
    assert messages == []
    assert stats["anchor_miss"] == 1
    assert stats["returned"] == 4


def test_malformed_response_is_survivable():
    messages, stats = cues.parse_response("not json at all", facts(), segment())
    assert messages == []
    assert stats["returned"] == 0


def test_dedupe_drops_messages_citing_the_identical_fact_set():
    messages = [
        {"t_ms": 1000, "confidence": 0.9, "kind": "production", "fact_ids": ["f2"],
         "text": "Jordan Downey directed the film."},
        {"t_ms": 5000, "confidence": 0.8, "kind": "production", "fact_ids": ["f2"],
         "text": "Downey directed the film."},
    ]
    assert len(cues.dedupe(messages)) == 1


def test_dedupe_keeps_distinct_notes_that_share_one_fact_id():
    # Both notes cite f1 (the lead actor), but they say different things: a
    # shared fact id alone must not collapse them.
    messages = [
        {"t_ms": 1000, "confidence": 0.9, "kind": "cast", "fact_ids": ["f1"],
         "text": "Harrison Ford took the part after reading the script in one sitting."},
        {"t_ms": 900_000, "confidence": 0.8, "kind": "craft", "fact_ids": ["f1", "f2"],
         "text": "Ford broke his ankle on the door of the Falcon set and kept filming."},
    ]
    assert len(cues.dedupe(messages)) == 2


def test_dedupe_drops_rephrased_text_without_shared_ids():
    messages = [
        {"t_ms": 1000, "confidence": 0.9, "kind": "production", "fact_ids": [],
         "text": "The Cycle was produced by James Harris and Mark Lane."},
        {"t_ms": 600_000, "confidence": 0.8, "kind": "production", "fact_ids": [],
         "text": "The Cycle was produced by James Harris, Mark Lane and Michael Downey."},
    ]
    assert len(cues.dedupe(messages)) == 1


def test_dedupe_keeps_distinct_facts():
    messages = [
        {"t_ms": 1000, "confidence": 0.9, "kind": "cast", "fact_ids": ["f1"],
         "text": "Don Keith Opper plays Frank."},
        {"t_ms": 600_000, "confidence": 0.9, "kind": "cast", "fact_ids": ["f4"],
         "text": "Carl Crudup plays Lou Bishop."},
    ]
    assert len(cues.dedupe(messages)) == 2


def test_density_keeps_the_stronger_message_in_a_window():
    messages = [
        {"t_ms": 1000, "confidence": 0.7, "kind": "cast", "subject": "a"},
        {"t_ms": 2000, "confidence": 0.95, "kind": "craft", "subject": "b"},
        {"t_ms": 900_000, "confidence": 0.8, "kind": "music", "subject": "c"},
    ]
    kept = cues.apply_density(messages, min_gap_ms=360_000)
    assert [m["subject"] for m in kept] == ["b", "c"]


def test_generate_uses_injected_model_and_returns_anchored_messages():
    import re

    # Han Solo is spoken at 5s, so the cast claim is locked to that line and
    # unlocked by this segment's end; the note carries the reveal annotation.
    lines = LINES + [{"start_ms": 5000, "end_ms": 7000, "text": "Han Solo is gone."}]
    seg = {"start_ms": 0, "end_ms": 10_000, "lines": lines}

    def complete(prompt: str) -> str:
        transcript = [line for line in prompt.splitlines() if re.match(r"^\[\d+\] ", line)]
        anchor = transcript[0].split("] ", 1)[1]
        return response({"messages": [{
            "anchor": anchor, "kind": "cast", "text": "A sourced fact.",
            "fact_ids": ["f1"], "confidence": 0.9, "subject": "han",
        }]})

    messages, stats, withheld = cues.generate([seg], facts(), complete)
    assert len(messages) == 1
    assert stats["kept"] == 1
    assert stats["anchor_hit_rate"] == 1.0
    assert messages[0]["reveal_state"] == "locked"
    assert messages[0]["unlock_anchor_ms"] == 5000
    assert messages[0]["subject_key"] == "han solo"
    # The locked claim is listed in the credits artifact with its unlock time.
    assert [(w["claim_id"], w["unlock_anchor_ms"]) for w in withheld] == [("f1", 5000)]


def test_prompt_bounds_spoilers_and_model_only_knowledge():
    prompt = cues.build_prompt(segment(), facts(), "transcript")
    assert "later in this title" in prompt
    assert "non-plot material" in prompt
    assert "not evidence about the plot" in prompt


def test_live_stream_is_sourced_only_and_model_only_notes_move_to_credits():
    # Live is stricter than the aftershow: every streaming note cites a fact.
    # Model-only notes are not dropped silently — they move to the credits
    # set, where the same 30% share cap applies against the grounded claims.
    lines = ["He was a good friend.", "The shield generator is down.",
             "A long way later.", "We should get moving."]
    starts = [0, 20_000, 400_000, 600_000]
    segments = [
        {"start_ms": start, "end_ms": start + 10_000,
         "lines": [{"start_ms": start + 1000, "end_ms": start + 3000, "text": lines[i]}]}
        for i, start in enumerate(starts)
    ]
    sheet = [
        {"id": f"f{i + 1}", "text": f"Fact {i + 1}", "source": "wikipedia",
         "url": "https://en.wikipedia.org/wiki/X"}
        for i in range(4)
    ]
    grounded = [
        "The desert opening was filmed in Tunisia across six punishing summer weeks.",
        "Production designer Norman Garwood built the farmhouse interior on a soundstage.",
        "Composer John Williams recorded the score with a ninety piece orchestra.",
        "Editor Paul Hirsch cut the climactic battle to under four minutes.",
    ]
    unverified = [
        "The masked raider is secretly the hero's father according to fan theories.",
        "The sidekick dies during the escape sequence in the novelization.",
    ]

    def complete(prompt: str) -> str:
        import re

        header = re.search(r"SEGMENT: (\d+)s to", prompt)
        index = starts.index(int(header.group(1)) * 1000)
        anchor = lines[index]
        payload = {"messages": [{
            "anchor": anchor, "kind": "craft", "text": grounded[index],
            "fact_ids": [f"f{index + 1}"], "confidence": 0.7,
        }]}
        if index < len(unverified):
            payload["messages"].append({
                "anchor": anchor, "kind": "context", "text": unverified[index],
                "fact_ids": [], "confidence": 0.95 if index == 0 else 0.9,
            })
        return response(payload)

    messages, stats, withheld = cues.generate(segments, sheet, complete)
    # Density keeps the strongest note per window; the early window's survivor
    # is the unverified one, so only the two late sourced notes stream live.
    assert len(messages) == 2
    assert all(not m["model_only"] for m in messages)
    assert stats["withheld_model_only"] == 0  # 30% of an empty claim set is zero


def test_credits_set_keeps_model_only_notes_up_to_the_cap():
    lines = ["He was a good friend.", "A long way later."]
    starts = [0, 400_000]
    segments = [
        {"start_ms": start, "end_ms": start + 10_000,
         "lines": [{"start_ms": start + 1000, "end_ms": start + 3000, "text": lines[i]}]}
        for i, start in enumerate(starts)
    ]
    sheet = [
        {"id": "f1", "text": "Harrison Ford plays Han Solo.", "source": "plex",
         "url": None},  # Han Solo is never spoken -> credits claim
        {"id": "f2", "text": "Shot on location in Tunisia.", "source": "wikipedia",
         "url": "https://en.wikipedia.org/wiki/X"},
        {"id": "f3", "text": "Mark Hamill plays Luke Skywalker.", "source": "plex",
         "url": None},  # never spoken either
        {"id": "f4", "text": "Carrie Fisher plays Leia Organa.", "source": "plex",
         "url": None},  # third credits claim: cap allows one model-only note
    ]

    def complete(prompt: str) -> str:
        import re

        transcript = [line for line in prompt.splitlines() if re.match(r"^\[\d+\] ", line)]
        anchor = transcript[0].split("] ", 1)[1]
        return response({"messages": [{
            "anchor": anchor, "kind": "cast", "text": "An unverified aside.",
            "fact_ids": [], "confidence": 0.95,
        }]})

    messages, stats, withheld = cues.generate(segments, sheet, complete)
    assert messages == []  # both model notes are model-only; live stays sourced
    claim_entries = [w for w in withheld if not w.get("model_only")]
    note_entries = [w for w in withheld if w.get("model_only")]
    assert len(claim_entries) == 3
    assert len(note_entries) == int(3 * 0.3 / 0.7) == 1  # the 30% cap
    assert stats["withheld"] == 4
    assert note_entries[0]["text"] == "An unverified aside."
    total = len(withheld)
    assert len(note_entries) * 10 <= total * 3


def test_generate_survives_a_failing_model():
    def complete(prompt: str) -> str:
        raise RuntimeError("provider down")

    messages, stats, withheld = cues.generate([segment()], facts(), complete)
    assert messages == []
    # Withheld claims exist even when the model fails: the cast claim was
    # quarantined at classification time, before any model call.
    assert [(w["claim_id"], w["reveal_state"]) for w in withheld] == [("f1", "credits")]
    assert stats["segments"] == 0


# --- R2: fact-sheet slot allocation -----------------------------------------

def big_metadata():
    return {
        "title": "The Cycle",
        "Director": [{"tag": "Jordan Downey"}],
        "Writer": [{"tag": "Jay Littleton"}],
        "Producer": [{"tag": "James Harris"}],
        "studio": "BoulderLight Pictures",
        "year": 2026,
        "Role": [
            {"tag": "Deborah Ann Woll", "role": "Meg"},
            {"tag": "Jeffrey Donovan", "role": "Marks"},
            {"tag": "Nobody Credited", "role": ""},
            {"tag": "Voice One", "role": "Additional Voices"},
            {"tag": "Voice Two", "role": "additional voices"},
            {"tag": "Young Meg", "role": "Meg (credited as Margaret)"},
            {"tag": "Don Keith Opper", "role": "Frank"},
            {"tag": "Carl Crudup", "role": "Lou Bishop"},
            {"tag": "Jeb Berrier", "role": "Joe Ratterman"},
            {"tag": "Jordyn Allen", "role": "Young Meg"},
        ],
    }


def wiki_facts(n: int) -> list[dict]:
    return [{"text": f"Wikipedia production fact number {i} about the making of the film.",
             "section": "production"} for i in range(n)]


def test_fact_sheet_drops_vague_cast_lines_outright():
    sheet = cues.build_fact_sheet(big_metadata(), wiki_facts(2))
    texts = "\n".join(f["text"] for f in sheet)
    assert "plays a role" not in texts
    assert "Additional Voices" not in texts
    assert "additional voices" not in texts
    assert "credited as" not in texts
    # Real role lines still land.
    assert any("Deborah Ann Woll plays Meg." == f["text"] for f in sheet)


def test_fact_sheet_caps_plex_cast_at_eight():
    metadata = {"Role": [{"tag": f"Actor {i}", "role": f"Role {i}"} for i in range(20)]}
    sheet = cues.build_fact_sheet(metadata, [])
    cast_lines = [f for f in sheet if f["text"].startswith("Actor")]
    assert len(cast_lines) == 8


def test_fact_sheet_adds_free_plex_metadata_last():
    # Lever 5 (artifacts/phase6/): tagline, genres, countries, audience rating
    # and collections are already in the Plex response — free facts, no
    # network call, spoiler-safe by construction.
    metadata = dict(big_metadata(), tagline="You can't bury the past forever.",
                    Genre=[{"tag": "Horror"}, {"tag": "Thriller"}],
                    Country=[{"tag": "United States of America"}, {"tag": "United Kingdom"}],
                    audienceRating=6.7,
                    Collection=[{"tag": "Halloween 2026"}])
    sheet = cues.build_fact_sheet(metadata, wiki_facts(1))
    texts = [f["text"] for f in sheet]
    for expected in ('Tagline: "You can\'t bury the past forever."',
                     "Genres: Horror, Thriller.",
                     "Countries: United States of America, United Kingdom.",
                     "Plex audience rating: 6.7/10.",
                     "Collections: Halloween 2026."):
        assert expected in texts
    new = [f for f in sheet if f["text"] in (
        'Tagline: "You can\'t bury the past forever."',
        "Genres: Horror, Thriller.",
        "Countries: United States of America, United Kingdom.",
        "Plex audience rating: 6.7/10.",
        "Collections: Halloween 2026.")]
    assert all(f["source"] == "plex" for f in new)
    # They come after every credit line, so a capped sheet loses them first.
    last_credit = max(i for i, f in enumerate(sheet)
                      if f["text"].startswith(("Released in", "Studio:"))
                      or " plays " in f["text"])
    assert min(texts.index(f["text"]) for f in new) > last_credit


def test_fact_sheet_omits_absent_plex_metadata():
    sheet = cues.build_fact_sheet({"title": "X"}, [])
    texts = "\n".join(f["text"] for f in sheet)
    assert "Tagline" not in texts
    assert "Genres" not in texts
    assert "Countries" not in texts
    assert "audience rating" not in texts
    assert "Collections" not in texts


def test_fact_sheet_capped_sheet_still_prefers_wikipedia_facts():
    # A rich title fills the sheet with Wikipedia facts; the free Plex lines
    # must never displace one (they sit below the cap line).
    metadata = dict(big_metadata(), tagline="A tagline.",
                    Genre=[{"tag": "Horror"}], audienceRating=6.7)
    sheet = cues.build_fact_sheet(metadata, wiki_facts(60))
    assert len(sheet) == 60
    assert all(f["source"].startswith("wikipedia") for f in sheet)


def test_fact_sheet_orders_wikipedia_and_person_facts_first_plex_last():
    sheet = cues.build_fact_sheet(
        big_metadata(), wiki_facts(3), person_facts=["Someone is known for X."])
    sources = [f["source"] for f in sheet]
    plex_indexes = [i for i, s in enumerate(sources) if s == "plex"]
    wiki_indexes = [i for i, s in enumerate(sources) if s.startswith("wikipedia")]
    assert plex_indexes and wiki_indexes
    assert max(wiki_indexes) < min(plex_indexes)
    # Person facts are in the wikipedia block, ahead of plex.
    assert any(f["text"] == "Someone is known for X." for f in sheet)


def test_fact_sheet_attributes_sections_as_source_labels():
    sheet = cues.build_fact_sheet(
        {"title": "X"},
        [{"text": "Production began in March 2025 on a modest budget in Canada.",
          "section": "production"},
         {"text": "The film grossed four million dollars in its opening weekend.",
          "section": "box office"}])
    labels = {f["source"] for f in sheet}
    assert labels == {"wikipedia:production", "wikipedia:box office"}


def test_fact_sheet_default_budget_is_sixty_facts_with_stable_ids():
    sheet = cues.build_fact_sheet(big_metadata(), wiki_facts(60))
    assert len(sheet) == 60
    assert [f["id"] for f in sheet] == [f"f{i + 1}" for i in range(60)]
    # A second build with identical inputs yields identical ids.
    again = cues.build_fact_sheet(big_metadata(), wiki_facts(60))
    assert [f["id"] for f in again] == [f["id"] for f in sheet]


def test_excluded_wikipedia_sections_never_reach_a_prompt():
    # The mandatory spoiler gate: an article whose Reception section spoils the
    # ending must contribute nothing to any prompt built from the sheet.
    from watch_with_me.sources import wikipedia as wiki_source

    article = (
        "Dark Harvest is a 2023 American horror film directed by David Slade.\n"
        "== Production ==\n"
        "Production began in March 2022, with filming taking place in rural Manitoba.\n"
        "== Reception ==\n"
        "Critics praised the twist ending, in which the October Boy is the harvest.\n"
    )
    sheet = cues.build_fact_sheet(
        {"title": "Dark Harvest", "_wikipedia_url": "https://en.wikipedia.org/wiki/Dark_Harvest"},
        wiki_source.section_facts(article))
    assert all(f["source"] != "wikipedia:reception" for f in sheet)
    prompt = cues.build_prompt(segment(), sheet, "transcript")
    assert "twist ending" not in prompt
    assert "October Boy is the harvest" not in prompt


def test_density_default_gap_is_ninety_seconds():
    messages = [
        {"t_ms": 0, "confidence": 0.9, "kind": "cast", "subject": "a"},
        {"t_ms": 100_000, "confidence": 0.8, "kind": "craft", "subject": "b"},
        {"t_ms": 200_000, "confidence": 0.7, "kind": "music", "subject": "c"},
    ]
    kept = cues.apply_density(messages)
    assert [m["subject"] for m in kept] == ["a", "b", "c"]


def test_density_default_gap_still_prunes_inside_the_window():
    messages = [
        {"t_ms": 0, "confidence": 0.7, "kind": "cast", "subject": "a"},
        {"t_ms": 60_000, "confidence": 0.9, "kind": "craft", "subject": "b"},
    ]
    kept = cues.apply_density(messages)
    assert [m["subject"] for m in kept] == ["b"]
