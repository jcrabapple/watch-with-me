from __future__ import annotations

from watch_with_me.sources import wikipedia

EXTRACT = (
    "Production began in March 2025 on a budget of twelve million dollars, with "
    "location work planned for the Scottish Highlands. The shocking climax, filmed "
    "on location in Scotland, reveals that the detective was the killer. "
    "Cinematographer Jane Doe shot the night scenes using only natural light and "
    "practical lanterns."
)


def test_production_sentences_exclude_plot_vocabulary_anywhere_in_the_sentence():
    sentences = wikipedia.production_sentences(EXTRACT)
    # The climax sentence passes the old startswith filter; it must not survive.
    assert not any("climax" in s.lower() for s in sentences)
    assert not any("reveals" in s.lower() for s in sentences)
    # Genuine production sentences are untouched.
    assert any("Production began" in s for s in sentences)
    assert any("Cinematographer Jane Doe" in s for s in sentences)


# A miniature of the real article shape: a lead, a safe section, and the
# sections that must never reach a prompt even when their sentences pass
# every length/quote/quality check.
ARTICLE = """\
Dark Harvest is a 2023 American horror film directed by David Slade and written by Michael Gilio. The film stars Casey Likes and E'myri Crutchfield in the lead roles.

== Plot ==
The October Boy rises from the cornfield every autumn. In the shocking twist ending, the townspeople are revealed to be the real monsters all along.

== Production ==
Production began in March 2022, with filming taking place on location in rural Manitoba over twenty nights. Director David Slade insisted on practical cornfield sets rather than digital backdrops.

== Reception ==
Critics praised the twist ending, in which the October Boy is unmasked as the harvest itself. On review aggregator Rotten Tomatoes, the film holds a 72% approval rating.

== Critical response ==
Brian Tallerico of RogerEbert.com mentioned the "still out there" energy of the finale, in which every adult in town is complicit.

== Box office ==
The film grossed four million dollars against a production budget of twelve million dollars in its opening weekend.

== References ==
== External links ==
"""


def test_spoiler_sections_are_excluded_wholesale_even_when_clean():
    facts = wikipedia.section_facts(ARTICLE)
    sections = {f["section"] for f in facts}
    assert "plot" not in sections
    assert "reception" not in sections
    assert "critical response" not in sections
    assert "legacy" not in sections
    texts = "\n".join(f["text"] for f in facts)
    # No excluded-section sentence may reach a prompt, however clean it is.
    assert "twist ending" not in texts
    assert "72% approval" not in texts
    assert "still out there" not in texts


def test_safe_sections_are_kept_and_attributed():
    facts = wikipedia.section_facts(ARTICLE)
    by_section = {f["section"]: f for f in facts}
    assert by_section["production"]["section"] == "production"
    assert "practical cornfield sets" in by_section["production"]["text"]
    assert by_section["box office"]["section"] == "box office"
    assert "grossed four million" in by_section["box office"]["text"]
    # Every section in the output is either allow-listed or the lead;
    # nothing else (plot, reception, references…) may contribute.
    assert all(f["section"] in wikipedia.SAFE_SECTIONS + ("lead",) for f in facts)


def test_lead_is_kept_but_only_with_production_hints():
    facts = wikipedia.section_facts(ARTICLE)
    lead_texts = [f["text"] for f in facts if f["section"] == "lead"]
    assert any("directed by David Slade" in t for t in lead_texts)
    assert any("Casey Likes" in t for t in lead_texts)
    # Lead sentences with no production cue at all must not be kept.
    no_hints = wikipedia.section_facts(
        "A quiet town hides a dark secret every single autumn season. "
        "Everyone knows something is wrong but nobody says it out loud."
    )
    assert no_hints == []


def test_heading_fragments_are_rejected():
    # A nested sub-heading inside a safe section must not become sentence
    # content and take the cast list with it (the old "== Cast == Deborah Ann
    # Woll as Meg …" blob).
    extract = (
        "== Production ==\n"
        "Filming began in March 2025 in rural Manitoba with practical sets. "
        "=== Filming === "
        "Deborah Ann Woll as Meg joined the ensemble cast that spring."
    )
    facts = wikipedia.section_facts(extract)
    assert facts
    assert all("==" not in f["text"] for f in facts)
    assert all(not f["text"].lstrip().startswith("==") for f in facts)
    assert all("Deborah Ann Woll" not in f["text"] for f in facts)


def test_quote_shards_and_unbalanced_sentences_are_rejected():
    extract = (
        '== Production ==\n'
        'Brian Tallerico of RogerEbert.com mentioned: "the practical effects are '
        'still out there." Filming took place over twenty nights on location in '
        'rural Manitoba with practical cornfield sets built by the crew.'
    )
    facts = wikipedia.section_facts(extract)
    texts = [f["text"] for f in facts]
    # The quoted review sentence is kept whole, not split into shards.
    assert any("still out there." in t and "Filming took place" not in t for t in texts)
    assert any(t.startswith("Filming took place") for t in texts)

    shard = (
        '== Production ==\n'
        'still out there…" Brian Tallerico of RogerEbert.com mentioned: "…and the '
        'crew built every cornfield set by hand on location in rural Manitoba.'
    )
    shard_facts = wikipedia.section_facts(shard)
    assert all(f["text"].count('"') % 2 == 0 for f in shard_facts)
    # The leading shard (starts mid-quote) must not survive as a fact.
    assert all(not f["text"].startswith("still out there") for f in shard_facts)


def test_length_bounds_are_enforced():
    short = "== Production ==\nIt grossed well. Filming happened in Canada over many long cold winter nights with a large crew."
    facts = wikipedia.section_facts(short)
    assert all(len(f["text"]) >= 40 for f in facts)
    long_sentence = (
        "Production began in March 2025 with " + "location work planned " * 40
    )
    facts = wikipedia.section_facts(f"== Production ==\n{long_sentence}.")
    assert facts == []


def test_production_sentences_is_the_plain_text_view():
    sentences = wikipedia.production_sentences(ARTICLE)
    assert sentences == [f["text"] for f in wikipedia.section_facts(ARTICLE)][:12]


# --- rabbit-hole sentence scoring --------------------------------------------

def test_scorer_prefers_numbers_and_superlatives_over_definitions():
    assert wikipedia.score_sentence(
        "Uganda is a landlocked country in East Africa.") < 0
    assert wikipedia.score_sentence(
        "New Mexico is the fifth-largest state in the United States.") > 0
    assert wikipedia.score_sentence(
        "New Mexico has more PhDs per capita than any other state.") > 0
    assert wikipedia.score_sentence(
        "Filming began in March 2025 on a budget of twelve million dollars.") \
        > wikipedia.score_sentence("Its capital is Santa Fe.")


def test_scorer_penalises_as_of_dates_only_and_list_tails():
    assert wikipedia.score_sentence(
        "As of 2023, the population was 2.1 million.") < 0
    assert wikipedia.score_sentence(
        "Born March 12, 1987 (age 38) in London, England.") > 0
    # A bare parenthetical dates line carries no verb and nothing else.
    assert wikipedia.score_sentence("( 12 March 1987 )") < 0
    assert wikipedia.score_sentence(
        "and later appeared in several independent films,") < 0


def test_ranked_sentences_picks_interesting_over_first():
    extract = (
        "Uganda is a landlocked country in East Africa. "
        "Uganda gained independence from Britain in 1962. "
        "Its capital is Kampala."
    )
    ranked = wikipedia.ranked_sentences(extract)
    assert ranked[0] == "Uganda gained independence from Britain in 1962."
    assert not any("landlocked country" in s for s in ranked)


def test_is_place_article_and_place_fact():
    place = (
        "New Mexico is a state in the Southwestern United States. "
        "Its capital is Santa Fe. "
        "New Mexico has the highest percentage of Hispanic Americans of any state."
    )
    assert wikipedia.is_place_article(place)
    fact = wikipedia.place_fact(place)
    assert fact is not None
    assert "highest percentage" in fact

    person = "Carrie Preston is an American actress and producer."
    assert not wikipedia.is_place_article(person)


def test_place_fact_says_nothing_for_definition_only_articles():
    thin = (
        "Uganda is a landlocked country in East Africa. "
        "Its capital is Kampala. "
        "It is bordered by Kenya to the east."
    )
    assert wikipedia.place_fact(thin) is None
