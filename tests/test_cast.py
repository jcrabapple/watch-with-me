from __future__ import annotations

from watch_with_me.cues import build_fact_sheet
from watch_with_me.sources.cast import actor_facts, lead_sentence, person_facts

LEAD = ("Carrie Preston is an American actress and producer. She is best known for playing "
        "Arlene Fowler in True Blood and Elsbeth Tascioni in The Good Wife, a role that won "
        "her a Primetime Emmy Award.")


def test_lead_sentence_prefers_the_known_for_sentence():
    assert lead_sentence(LEAD, "Carrie Preston").startswith("She is best known for")


def test_lead_sentence_skips_plot_and_in_universe_text():
    assert lead_sentence("Elsbeth is a fictional character. The plot follows her cases.",
                         "Elsbeth Tascioni") is None


def test_actor_facts_pairs_role_with_biography():
    metadata = {"Role": [{"tag": "Carrie Preston", "role": "Elsbeth Tascioni"}]}
    (entry,) = actor_facts(metadata, lambda name: LEAD)
    assert entry["text"].startswith("Carrie Preston plays Elsbeth Tascioni.")
    assert "best known for" in entry["text"]
    assert entry["source_name"] == "wikipedia:Carrie Preston"


def test_actor_facts_survive_a_missing_article():
    def boom(name):
        raise OSError("network down")

    metadata = {"Role": [{"tag": "Nobody Known", "role": "Extra"}]}
    assert actor_facts(metadata, boom) == []


def test_actor_facts_respect_the_limit():
    metadata = {"Role": [{"tag": f"Actor {i}", "role": "Role"} for i in range(20)]}
    assert len(actor_facts(metadata, lambda name: LEAD, limit=3)) == 3


def test_person_facts_reach_the_fact_sheet():
    facts = build_fact_sheet({"title": "Elsbeth"}, [], person_facts=["Someone is known for X."])
    assert any(f["text"] == "Someone is known for X." for f in facts)
    assert [f["id"] for f in facts] == [f"f{i + 1}" for i in range(len(facts))]


# --- rabbit-hole: up to three facts per actor ---------------------------------

ARTICLE = (
    "Jordan Downey is an American filmmaker and actor. "
    "He directed the horror film ThanksKilling (2007) while still in college. "
    "He won the Audience Award at the 2019 Nightmares Film Festival for his film "
    "The Cycle. "
    "Downey grew up in Massachusetts and began his career editing wedding videos. "
    "In The Cycle, his character is revealed as the killer in the final act."
)


def test_person_facts_extract_role_award_and_bio():
    facts = person_facts(ARTICLE, "Jordan Downey")
    assert len(facts) == 3
    assert "ThanksKilling (2007)" in facts[0]  # notable role with a year
    assert "Audience Award" in facts[1]        # award sentence
    assert "wedding videos" in facts[2]        # biography oddity


def test_person_facts_never_mention_the_watched_title():
    facts = person_facts(ARTICLE, "Jordan Downey", title_keys=["the cycle"])
    assert facts
    assert all("The Cycle" not in f for f in facts)


def test_person_facts_skip_what_the_article_does_not_state():
    thin = "Nobody Famous is an American actor. He works in film."
    assert person_facts(thin, "Nobody Known") == []


def test_actor_facts_return_article_tagged_entries():
    metadata = {"title": "Some Unrelated Title",
                "Role": [{"tag": "Carrie Preston", "role": "Elsbeth Tascioni"}]}
    entries = actor_facts(metadata, lambda name: {"extract": LEAD,
                                                  "url": "https://en.wikipedia.org/wiki/Carrie_Preston",
                                                  "title": "Carrie Preston"})
    assert entries
    assert all(e["source_name"] == "wikipedia:Carrie Preston" for e in entries)
    assert all(e["url"].endswith("Carrie_Preston") for e in entries)
