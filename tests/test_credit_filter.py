from __future__ import annotations

from watch_with_me.cues import is_bare_credit


def test_bare_credit_is_rejected():
    assert is_bare_credit("Jeb Berrier plays coroner Joe Ratterman.")
    assert is_bare_credit("Jordyn Allen is credited as Young Meg.")


def test_credit_plus_a_fact_is_kept():
    assert not is_bare_credit(
        "Deborah Ann Woll plays Meg; she was Jessica Hamby in HBO's True Blood.")
    assert not is_bare_credit(
        "Jeffrey Donovan plays Marks. He starred for seven seasons on Burn Notice.")


def test_ordinary_context_note_is_kept():
    assert not is_bare_credit("The film premiered at Fantastic Fest in September 2026.")


def test_passive_voice_credits_are_rejected():
    assert is_bare_credit("Han Solo is played by Harrison Ford.")
    assert is_bare_credit("The coroner is portrayed by Jeb Berrier.")
    assert is_bare_credit("The dragon is voiced by Benedict Cumberbatch.")


def test_crew_credits_are_rejected():
    assert is_bare_credit("Directed by George Lucas.")
    assert is_bare_credit("Written by Lawrence Kasdan.")
    assert is_bare_credit("Produced by Gary Kurtz.")


def test_more_active_credit_verbs_are_rejected():
    assert is_bare_credit("Harrison Ford portrays Han Solo.")
    assert is_bare_credit("Mark Hamill voices Luke Skywalker.")
    assert is_bare_credit("Carrie Fisher stars as Leia.")


def test_credit_with_an_added_fact_is_kept():
    assert not is_bare_credit(
        "Han Solo is played by Harrison Ford, who broke his ankle on set.")
    assert not is_bare_credit(
        "Directed by George Lucas, who shot the dogfight table read on video.")
