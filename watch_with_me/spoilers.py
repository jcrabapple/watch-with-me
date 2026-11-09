"""Spoiler escrow: release states for fact-sheet claims.

Every claim (a numbered fact-sheet entry) carries a release state, and a
segment prompt receives only the claims that are unlocked at that segment's
end. Locked claims are never in the model's context, so a prompt or parser
failure cannot leak them; at the end of a session the withheld claims become
the "What we couldn't tell you yet" credits artifact.

Classification is deterministic prompt/schema work — no model calls:

- Plex crew/metadata lines (director, studio, year, tagline, genres, …) are
  safe by construction: ``live``.
- Plex cast lines ("X plays Y") are character claims and are never ``live``
  by default. Y is quarantined until the elapsed transcript first speaks the
  name: spoken at T -> ``locked`` with ``unlock_anchor_ms=T``; never spoken
  -> ``credits``, plus a companion ``live`` claim naming only the actor.
- Wikipedia claims are ``live``: upstream extraction (sources/wikipedia.py)
  is an allow-list of production-shaped sections with a spoiler-term filter,
  so anything that reaches the sheet is safe from minute one. Person facts
  (per-entity cast facts) are Wikipedia lead material and inherit the same
  guarantee.
- Anything else (a future source, or a hand-built sheet) falls back to
  subject quarantine: the first capitalised phrase is treated as the
  subject; first transcript mention locks it to that time, never-mentioned
  sends it to ``credits``.

Entity-expansion notes (Pass B ``context`` notes, ``source_name`` of
``wikipedia:<Entity>``) go through the same deterministic classifier
(:func:`classify_entity_note`) at build time, so no claim of either kind
reaches the page with a null release state.
"""

from __future__ import annotations

import re

REVEAL_LIVE = "live"
REVEAL_LOCKED = "locked"
REVEAL_CREDITS = "credits"
REVEAL_STATES = (REVEAL_LIVE, REVEAL_LOCKED, REVEAL_CREDITS)

# Model-only notes may not exceed this share of the credits set (the same
# 30% cap the live stream used to enforce).
CREDITS_MODEL_ONLY_RATIO = 0.3

# Shapes emitted by cues.build_fact_sheet for Plex-sourced lines.
_CAST_CLAIM = re.compile(r"^(?P<actor>.+?) plays (?P<role>.+?)\.$")
_METADATA_CLAIM = re.compile(
    r"^(?:Directed|Written|Produced) by .+$"
    r"|^Studio: .+$"
    r"|^Released in \d{4}\.$"
    r"|^Tagline: "
    r"|^Genres: .+$"
    r"|^Countries: .+$"
    r"|^Plex audience rating: .+$"
    r"|^Collections: .+$")

# A claim subject: a run of capitalised words (apostrophes/hyphens allowed).
# Leading sentence starters are skipped so "The film ..." yields "Film"'s
# neighbour rather than "The".
_SUBJECT = re.compile(r"[A-Z][\w'-]*(?: [A-Z][\w'-]*)*")
_SENTENCE_STARTERS = {
    "the", "a", "an", "in", "on", "at", "it", "this", "that", "he", "she",
    "they", "but", "and", "or", "as", "by", "for", "to", "of", "with",
}

# A title-level claim in an entity note: the note talks about the work being
# watched instead of the entity. The entity pass is instructed not to write
# these (NOTE_RULES); finding one is a prompt bug, so it is quarantined.
_TITLE_REFERENCE = re.compile(
    r"\b(?:the|this) (?:film|movie|picture|episode|show|series|title)\b",
    re.IGNORECASE)


def _normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _first_mention_ms(lines: list[dict], name: str) -> int | None:
    """First transcript line that speaks `name` (word-boundary match), or None."""
    key = _normalise(name)
    if not key:
        return None
    pattern = re.compile(rf"(?<!\w){re.escape(key)}(?!\w)")
    for line in lines:
        if pattern.search(_normalise(line.get("text") or "")):
            return int(line["start_ms"])
    return None


def _subject_key(text: str) -> str | None:
    for match in _SUBJECT.finditer(text):
        word = match.group(0).split()[0].lower().strip("'-.")
        if word in _SENTENCE_STARTERS:
            continue
        token = match.group(0)
        # A possessive ("Zoe's locket") names Zoe, not "Zoe s".
        if token.lower().endswith("'s"):
            token = token[:-2]
        return _normalise(token)
    return None


def classify_claims(facts: list[dict], transcript_lines: list[dict]) -> tuple[list[dict], list[dict]]:
    """Annotate every claim with its release state; split off the withheld set.

    Returns (visible_claims, withheld). Visible claims keep their fact-sheet
    ids and gain ``reveal_state``, ``unlock_anchor_ms`` and ``subject_key``.
    Withheld claims carry the same fields plus ``claim_id`` (the fact id) and
    are what the end-of-session credits artifact renders. When a cast claim
    is credits-only, a companion live claim naming only the actor is appended
    to the visible set (fresh id: the fact id plus "a").
    """
    visible: list[dict] = []
    withheld: list[dict] = []
    companions: list[dict] = []

    for fact in facts:
        claim = dict(fact)
        source = str(fact.get("source") or "")
        text = str(fact.get("text") or "")
        cast = _CAST_CLAIM.match(text) if source == "plex" else None

        if cast is not None:
            role = cast.group("role").strip()
            actor = cast.group("actor").strip()
            claim["subject_key"] = _normalise(role)
            mention = _first_mention_ms(transcript_lines, role)
            if mention is None:
                # The character's name is never spoken: the role half of the
                # claim can never be shown; only the actor may be named live.
                claim["reveal_state"] = REVEAL_CREDITS
                claim["unlock_anchor_ms"] = None
                withheld.append(_withheld_entry(claim))
                companions.append({
                    "id": f"{fact['id']}a",
                    "text": f"{actor} is in the cast.",
                    "source": source,
                    "url": fact.get("url"),
                    "reveal_state": REVEAL_LIVE,
                    "unlock_anchor_ms": None,
                    "subject_key": _normalise(actor),
                })
            else:
                claim["reveal_state"] = REVEAL_LOCKED
                claim["unlock_anchor_ms"] = mention
                visible.append(claim)
                withheld.append(_withheld_entry(claim))
        elif source == "plex" and _METADATA_CLAIM.match(text):
            claim["reveal_state"] = REVEAL_LIVE
            claim["unlock_anchor_ms"] = None
            claim["subject_key"] = _subject_key(text)
            visible.append(claim)
        elif source == "wikipedia" or source.startswith("wikipedia:"):
            claim["reveal_state"] = REVEAL_LIVE
            claim["unlock_anchor_ms"] = None
            claim["subject_key"] = _subject_key(text)
            visible.append(claim)
        else:
            # Unknown source: quarantine the claim on its subject.
            key = _subject_key(text)
            claim["subject_key"] = key
            mention = _first_mention_ms(transcript_lines, key) if key else None
            if key is None or mention is None:
                claim["reveal_state"] = REVEAL_CREDITS
                claim["unlock_anchor_ms"] = None
                withheld.append(_withheld_entry(claim))
            else:
                claim["reveal_state"] = REVEAL_LOCKED
                claim["unlock_anchor_ms"] = mention
                visible.append(claim)
                withheld.append(_withheld_entry(claim))

    return visible + companions, withheld


def _withheld_entry(claim: dict) -> dict:
    return {
        "claim_id": claim["id"],
        "text": claim["text"],
        "source_name": claim.get("source"),
        "source_url": claim.get("url"),
        "reveal_state": claim["reveal_state"],
        "unlock_anchor_ms": claim["unlock_anchor_ms"],
        "subject_key": claim.get("subject_key"),
        "model_only": 0,
    }


def _mentions_text(key: str, text: str) -> bool:
    """Word-boundary search of a normalised key in normalised text."""
    if not key or not text:
        return False
    pattern = re.compile(rf"(?<!\w){re.escape(key)}(?!\w)")
    return bool(pattern.search(_normalise(text)))


def _source_entity_name(note: dict) -> str:
    """The entity name carried by ``source_name`` (``wikipedia:<Entity>``)."""
    source = str(note.get("source_name") or "")
    if source.startswith("wikipedia:"):
        return source.split(":", 1)[1].strip()
    return ""


def classify_entity_note(note: dict, transcript_lines: list[dict], *,
                         title: str | None = None) -> dict:
    """Annotate an entity-expansion note with its release state.

    The note exists only because an anchor line named the entity, so by
    construction the mention is safe; classification makes that explicit
    instead of assumed (the Projectionist consumes unlocked claims):

    - ``credits`` when the text makes a claim about the title itself rather
      than the entity — detected deterministically by the title appearing in
      the text or a "the film/this episode"-style reference. The entity pass
      is instructed not to write these; any hit is a prompt bug and is
      reported via the build totals, never shown live.
    - ``live`` with ``unlock_anchor_ms`` at the first transcript line that
      names the entity, when that line is at or before the note's own time.
      When the full entity name never verbatim appears (the transcript may
      only partial-match a multi-word name), the note's own resolved anchor
      line is the mention evidence and sets the anchor.
    - ``locked`` when the first naming line is after the note's time — the
      impossible-by-construction case; quarantined rather than trusted.
    """
    note = dict(note)
    text = str(note.get("text") or "")
    name = str(note.get("subject") or "").strip() or _source_entity_name(note)
    key = _normalise(name)
    note["subject_key"] = key or None

    if (title and _mentions_text(_normalise(title), text)) \
            or _TITLE_REFERENCE.search(text):
        note["reveal_state"] = REVEAL_CREDITS
        note["unlock_anchor_ms"] = None
        return note

    mention = _first_mention_ms(transcript_lines, name) if key else None
    if mention is None:
        # The transcript partial-matched the name (e.g. "sovereign citizen
        # militia" for Sovereign citizen movement): the resolved anchor line
        # is the naming evidence, and it is by definition not after the note.
        mention = int(note.get("t_ms") or 0)
    note["unlock_anchor_ms"] = mention
    note["reveal_state"] = (REVEAL_LOCKED if mention > int(note.get("t_ms") or 0)
                            else REVEAL_LIVE)
    return note


def withheld_note_entry(note: dict) -> dict:
    """A withheld entity note in the credits-artifact entry shape."""
    return {
        "claim_id": note["id"],
        "text": note["text"],
        "source_name": note.get("source_name"),
        "source_url": note.get("source_url"),
        "reveal_state": note["reveal_state"],
        "unlock_anchor_ms": note.get("unlock_anchor_ms"),
        "subject_key": note.get("subject_key"),
        "model_only": 0,
    }


def claim_visible(claim: dict, segment_end_ms: int) -> bool:
    """Whether a claim may appear in a prompt for a segment ending at `segment_end_ms`.

    Claims without classification fields are treated as live, so callers that
    build sheets by hand (tests, one-off scripts) keep the old behaviour.
    """
    if claim.get("reveal_state", REVEAL_LIVE) == REVEAL_LIVE:
        return True
    anchor = claim.get("unlock_anchor_ms")
    return anchor is not None and anchor <= segment_end_ms


def visible_claims(facts: list[dict], segment_end_ms: int) -> list[dict]:
    return [f for f in facts if claim_visible(f, segment_end_ms)]


def cap_model_only_notes(withheld_claims: list[dict], notes: list[dict],
                         ratio: float = CREDITS_MODEL_ONLY_RATIO) -> list[dict]:
    """The credits set: withheld claims plus at most `ratio` model-only notes.

    The same share formula the live stream's cap used: with G grounded
    entries at most int(G * ratio / (1 - ratio)) unverified ones, so the
    unverified share of the final set never exceeds `ratio`.
    """
    limit = int(len(withheld_claims) * ratio / max(1e-6, 1 - ratio)) if ratio < 1 else len(notes)
    return list(withheld_claims) + list(notes)[:limit]
