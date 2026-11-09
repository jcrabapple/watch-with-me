"""Cue generation: fact sheet, prompt, response validation, anchor resolution.

The model never emits a timestamp. It emits a verbatim line from the supplied
transcript, and this module resolves that line to a time. A cue whose anchor
cannot be found is dropped, which is the only defence against invented times.
"""

from __future__ import annotations

import difflib
import hashlib
import re

from . import spoilers

KINDS = {"cast", "filmography", "production", "craft", "context", "award", "music"}
MAX_TEXT = 240
MAX_ANCHOR = 120
ANCHOR_RATIO = 0.86
MIN_CONFIDENCE = 0.65

SYSTEM_RULES = """You write short, factual film trivia notes that appear while someone watches.
Rules:
- Output JSON only, exactly this shape:
  {"messages": [{"anchor": "a line copied from the transcript", "kind": "cast",
                 "text": "One or two sentences.", "fact_ids": ["f3"],
                 "subject": "short-topic-key", "confidence": 0.9}]}
- "confidence" is required, a number from 0 to 1.
- Each message is one or two sentences, at most 240 characters.
- "anchor" must be copied verbatim from the supplied transcript, 120 characters or fewer.
- Use only facts listed in FACT SHEET. Put their ids in "fact_ids". Never invent an id or a URL.
- If a fact is general knowledge but not in the fact sheet, you may still use it, but leave
  "fact_ids" empty. Those are marked unverified. This allowance is for non-plot material
  only: production, casting, and release facts, never story developments.
- Never state anything that happens later in this title. If a fact could only be known
  from later events, omit it, even if the fact sheet or your own knowledge includes it.
- A character name from the cast list is metadata, not evidence about the plot; do not
  treat a credited role name as information about the story.
- No plot summary. No themes. No jokes unless the fact itself is funny.
- Nothing may depend on what happens after this segment ends. You have not been shown it.
- Zero messages is a correct answer when nothing is genuinely interesting.
- Return up to three messages per segment when the fact sheet supports three
  distinct facts. Never pad a segment to hit a number. If you return more than
  one, anchor them to different lines at least 60 seconds apart; two notes on
  the same line are one note.
- Never repeat, rephrase, or re-credit something already in ALREADY SAID.
- A bare release year, a studio name on its own, or "X directed and wrote this" is filler
  unless it is the only interesting thing you have. Prefer a specific, surprising detail.
- When a fact says what a credited actor is known for outside this title, that is usually
  the strongest note in the segment: name the role or show, not "is an actor".
- No "echoes", "resembles", or "is a nod to" comparisons. State what is known; do not
  assert a connection the sources do not make.
- "kind" must be exactly one of: cast, filmography, production, craft, context, award, music.
"""


def _normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def resolve_anchor(anchor: str, lines: list[dict], min_ratio: float = ANCHOR_RATIO) -> dict | None:
    """Find the transcript line an anchor refers to; None if it is not there."""
    target = _normalise(anchor)
    if not target or not lines:
        return None
    best_line = None
    best_ratio = 0.0
    for line in lines:
        ratio = difflib.SequenceMatcher(None, target, _normalise(line["text"])).ratio()
        if ratio > best_ratio:
            best_line, best_ratio = line, ratio
    if best_line is None or best_ratio < min_ratio:
        return None
    return {"t_ms": int(best_line["start_ms"]), "ratio": round(best_ratio, 3)}


MAX_PLEX_CAST = 8

# Role strings that carry no information: a missing role, voice-ensemble
# padding, or an alternate-credit parenthetical. These lines used to eat
# fact-sheet slots and were then deleted downstream by is_bare_credit; they
# never enter the sheet now.
WEAK_ROLES = ("", "a role", "additional voices", "additional voice")


def _weak_role(role: str | None) -> bool:
    flat = " ".join((role or "").split()).lower()
    return flat in WEAK_ROLES or "credited as" in flat


def build_fact_sheet(metadata: dict, wikipedia, *, max_facts: int = 60,
                     person_facts: list[str] | None = None) -> list[dict]:
    """Numbered facts from Wikipedia sentences and Plex metadata.

    Order is richest-source-first: Wikipedia production facts (section-attributed)
    and person facts, then Plex credits last. Plex cast lines are capped at
    MAX_PLEX_CAST and vague roles are dropped outright. `wikipedia` entries may
    be plain strings (labelled ``wikipedia:article``) or dicts with ``text``
    and ``section`` (labelled ``wikipedia:<section>``). Ids are assigned in
    insertion order, so they are stable for identical inputs.
    """
    facts: list[dict] = []

    def add(text: str, source: str, url: str | None = None) -> None:
        text = " ".join(text.split())
        if not text or len(facts) >= max_facts:
            return
        facts.append({"id": f"f{len(facts) + 1}", "text": text, "source": source, "url": url})

    for entry in wikipedia or []:
        if isinstance(entry, dict):
            text, section = entry.get("text") or "", entry.get("section") or "article"
        else:
            text, section = entry, "article"
        add(text, f"wikipedia:{section}", metadata.get("_wikipedia_url"))

    for line in person_facts or []:
        if isinstance(line, dict):
            add(line.get("text") or "", line.get("source_name") or "wikipedia",
                line.get("url"))
        else:
            add(line, "wikipedia", None)

    cast_lines = 0
    for role in metadata.get("Role") or []:
        if cast_lines >= MAX_PLEX_CAST:
            break
        name = (role.get("tag") or "").strip()
        part = " ".join((role.get("role") or "").split())
        if not name or _weak_role(part):
            continue
        before = len(facts)
        add(f"{name} plays {part}.", "plex", None)
        cast_lines += len(facts) > before
    for key, label in (("Director", "directed by"), ("Writer", "written by"),
                       ("Producer", "produced by")):
        for person in metadata.get(key) or []:
            if person.get("tag"):
                add(f"{label.capitalize()} {person['tag']}.", "plex", None)
    if metadata.get("studio"):
        add(f"Studio: {metadata['studio']}.", "plex", None)
    if metadata.get("year"):
        add(f"Released in {metadata['year']}.", "plex", None)
    # Free Plex metadata facts: no network call, spoiler-safe by construction.
    # Measured in artifacts/phase6/ as lever 5 — +4 fact lines on both the
    # thin (The Cycle) and rich (Sinners) controls. They sit after every
    # credit line, so a fact sheet that hits max_facts loses these first,
    # never Wikipedia material (thin titles are exactly the sheets with room).
    if metadata.get("tagline"):
        add(f"Tagline: \"{metadata['tagline']}\"", "plex", None)
    genres = [g.get("tag") for g in (metadata.get("Genre") or []) if g.get("tag")]
    if genres:
        add("Genres: " + ", ".join(genres) + ".", "plex", None)
    countries = [c.get("tag") for c in (metadata.get("Country") or []) if c.get("tag")]
    if countries:
        add("Countries: " + ", ".join(countries) + ".", "plex", None)
    if metadata.get("audienceRating") is not None:
        add(f"Plex audience rating: {metadata['audienceRating']}/10.", "plex", None)
    collections = [c.get("tag") for c in (metadata.get("Collection") or []) if c.get("tag")]
    if collections:
        add("Collections: " + ", ".join(collections) + ".", "plex", None)

    return facts


def build_prompt(segment: dict, facts: list[dict], transcript: str,
                 already_said: list[str] | None = None) -> str:
    already_said = already_said or []
    # Spoiler escrow: a segment prompt receives only claims unlocked at the
    # segment end. Locked claims are absent from the prompt entirely, and
    # credits claims never appear in any prompt.
    facts = spoilers.visible_claims(facts, segment["end_ms"])
    fact_lines = "\n".join(f"{f['id']} ({f['source']}): {f['text']}" for f in facts)
    said = "\n".join(f"- {text}" for text in already_said[-12:]) or "(nothing yet)"
    return (
        f"{SYSTEM_RULES}\n"
        f"SEGMENT: {segment['start_ms'] // 1000}s to {segment['end_ms'] // 1000}s\n"
        f"ALREADY SAID (do not repeat or rephrase any of these):\n{said}\n\n"
        f"FACT SHEET\n{fact_lines or '(none)'}\n\n"
        f"TRANSCRIPT (do not use anything after the segment end)\n{transcript}\n"
    )


def parse_response(raw: str, facts: list[dict], segment: dict) -> tuple[list[dict], dict]:
    """Validate a model response against the schema; returns (messages, stats).

    Every rejection records a reason. A single "rejected" count hides whether the
    model produced bad kinds, long text, or a missing field, and that difference
    decides whether to change the prompt or the parser.
    """
    import collections
    import json

    reasons: collections.Counter = collections.Counter()
    stats = {"returned": 0, "anchor_miss": 0, "rejected": 0, "reasons": {}}
    if not raw.strip():
        reasons["empty_response"] += 1
        stats["reasons"] = dict(reasons)
        return [], stats
    try:
        payload = json.loads(_extract_json(raw))
    except ValueError:
        reasons["unparsable_json"] += 1
        stats["reasons"] = dict(reasons)
        return [], stats
    items = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        reasons["no_messages_array"] += 1
        stats["reasons"] = dict(reasons)
        return [], stats

    known = {f["id"]: f for f in facts}
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            stats["rejected"] += 1
            reasons["not_an_object"] += 1
            continue
        stats["returned"] += 1
        anchor = str(item.get("anchor") or "").strip()
        text = " ".join(str(item.get("text") or "").split())
        kind = str(item.get("kind") or "").strip().lower()
        if not anchor:
            stats["rejected"] += 1
            reasons["missing_anchor"] += 1
            continue
        if len(anchor) > MAX_ANCHOR:
            stats["rejected"] += 1
            reasons["anchor_too_long"] += 1
            continue
        if not text:
            stats["rejected"] += 1
            reasons["missing_text"] += 1
            continue
        if len(text) > MAX_TEXT:
            stats["rejected"] += 1
            reasons["text_too_long"] += 1
            continue
        if kind not in KINDS:
            stats["rejected"] += 1
            reasons[f"bad_kind:{kind[:20]}"] += 1
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < MIN_CONFIDENCE:
            stats["rejected"] += 1
            reasons["low_confidence"] += 1
            continue
        resolved = resolve_anchor(anchor, segment["lines"])
        if resolved is None:
            stats["anchor_miss"] += 1
            reasons["anchor_miss"] += 1
            continue
        fact_ids = [fid for fid in item.get("fact_ids") or [] if fid in known]
        # Spoiler escrow: citing a claim that is not unlocked at this segment
        # is a leak attempt (the model can only know the id from a previous
        # prompt or a guess). Reject the whole message: dropping just the id
        # would leave the spoiler text standing with a "sourced" label.
        if any(not spoilers.claim_visible(known[fid], segment["end_ms"]) for fid in fact_ids):
            stats["rejected"] += 1
            reasons["withheld_fact_cited"] += 1
            continue
        cited = [known[fid] for fid in fact_ids]
        anchors = [c["unlock_anchor_ms"] for c in cited
                   if c.get("unlock_anchor_ms") is not None]
        out.append({
            "id": _message_id(segment, anchor, text),
            "t_ms": resolved["t_ms"],
            "kind": kind,
            "text": text,
            "anchor": anchor,
            "anchor_ratio": resolved["ratio"],
            "fact_ids": fact_ids,
            "model_only": 0 if fact_ids else 1,
            "source_name": known[fact_ids[0]]["source"] if fact_ids else None,
            "source_url": known[fact_ids[0]]["url"] if fact_ids else None,
            "confidence": confidence,
            "subject": str(item.get("subject") or "")[:80],
            "reveal_state": spoilers.REVEAL_LOCKED if anchors else spoilers.REVEAL_LIVE,
            "unlock_anchor_ms": max(anchors) if anchors else None,
            "subject_key": next((c.get("subject_key") for c in cited if c.get("subject_key")),
                                None),
        })
    stats["reasons"] = dict(reasons)
    return out, stats


def _extract_json(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no json object")
    return raw[start:end + 1]


def _message_id(segment: dict, anchor: str, text: str) -> str:
    digest = hashlib.sha1(f"{segment['start_ms']}:{anchor}:{text}".encode()).hexdigest()[:12]
    return f"msg_{digest}"


def _parse_failed(stats: dict) -> bool:
    reasons = stats.get("reasons") or {}
    return any(reason in reasons for reason in
               ("unparsable_json", "empty_response", "no_messages_array"))


def _retryable(stats: dict) -> bool:
    """A transport-shaped failure is worth one more call; a schema failure is not."""
    return _parse_failed(stats)


def dedupe(messages: list[dict], *, similarity: float = 0.55) -> list[dict]:
    """Drop repeated facts, however the model rephrases them.

    Two messages are duplicates when they cite the same fact id or when their texts
    share enough vocabulary. The model's own `subject` string is not a reliable key:
    it varies between calls for the same fact.
    """
    kept: list[dict] = []
    for message in sorted(messages, key=lambda m: (m["t_ms"], -m["confidence"])):
        if any(_same_fact(message, other, similarity) for other in kept):
            continue
        kept.append(message)
    return kept


def _same_fact(a: dict, b: dict, similarity: float) -> bool:
    ids_a = set(a.get("fact_ids") or [])
    ids_b = set(b.get("fact_ids") or [])
    # Only an identical, non-empty fact set is evidence of the same note; a
    # single shared id (usually the lead actor or director) is not.
    if ids_a and ids_a == ids_b:
        return True
    return _similar_text(a.get("text", ""), b.get("text", ""), similarity)


def _similar_text(a: str, b: str, threshold: float) -> bool:
    left = set(_normalise(a).split())
    right = set(_normalise(b).split())
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= threshold


def apply_density(messages: list[dict], min_gap_ms: int = 90_000) -> list[dict]:
    """Keep the strongest message in each window so the stream stays readable.

    Ninety seconds is the rabbit-hole cadence: with dedupe removing repeats
    and every note grounded, a denser stream reads as a feed rather than a
    trickle. The earlier three-minute window threw away more than half of
    good material on a well-covered title.
    """
    kept: list[dict] = []
    for message in sorted(messages, key=lambda m: (m["t_ms"], -m["confidence"])):
        if kept and message["t_ms"] - kept[-1]["t_ms"] < min_gap_ms:
            if message["confidence"] > kept[-1]["confidence"]:
                kept[-1] = message
            continue
        kept.append(message)
    return kept


BARE_CREDIT = re.compile(
    r"^[A-Z][\w'-]+(?: [\w'-]+)* "
    r"(?:plays|is credited as|appears as|portrays|voices|stars as) [A-Za-z0-9 '-]+$")
PASSIVE_CREDIT = re.compile(
    r"^[A-Z][A-Za-z0-9 '-]+ is (?:played|portrayed|voiced) by "
    r"[A-Z][\w'-]+(?: [\w'-]+)*$")
CREW_CREDIT = re.compile(r"^(?:Directed|Written|Produced) by [A-Z][\w'-]+(?: [\w'-]+)*$")


def is_bare_credit(text: str) -> bool:
    """True for a credit and nothing else: a name, a role, or a crew line.

    Active ("X plays Y", "X portrays Y"), passive ("Y is played by X"), and
    crew ("Directed by X") forms are all filler when they carry no extra fact.
    """
    flat = " ".join(text.split())
    if flat.endswith("."):
        flat = flat[:-1]
    return bool(BARE_CREDIT.match(flat) or PASSIVE_CREDIT.match(flat)
                or CREW_CREDIT.match(flat))


def generate(segments: list[dict], facts: list[dict], complete,
             *, max_per_segment: int = 3, on_segment=None,
             expander=None, transcript_lines: list[dict] | None = None,
             title: str | None = None) -> tuple[list[dict], dict, list[dict]]:
    """Run the model per segment; return (messages, totals, withheld).

    `complete(prompt) -> str` is injected: the plugin passes its aux task, the
    build CLI passes whatever client it has, tests pass a stub. `on_segment`
    receives (index, total, running_totals) after each call so a long build can
    report progress instead of looking hung. `expander`, when given, is the
    entity-expansion pass (watch_with_me.entities.Expander): it runs after
    Pass A on segments that produced fewer than three notes and may add up to
    two grounded `context` notes; Pass A output is byte-identical either way.

    Spoiler escrow: claims are classified once against the full transcript
    (`transcript_lines`, gathered from the segments when not given) and each
    segment prompt receives only the claims unlocked at that segment's end.
    Entity notes from the expander go through
    `spoilers.classify_entity_note` with the same transcript: a note that
    comes out live streams with `unlock_anchor_ms` at its mention line; a
    locked/credits note never reaches the page and joins the credits
    artifact instead (`totals["entity_withheld"]` counts them; any non-zero
    value is a prompt bug worth reporting). The live stream is sourced-only —
    model-only notes are withheld into the credits set, capped at 30% of it.
    `withheld` (locked + credits claims plus the capped model-only notes) is
    what the end-of-session credits artifact renders; its entries carry
    claim_id/text/source/reveal_state fields.
    """
    from .transcript import render_segment

    if transcript_lines is None:
        transcript_lines = [line for segment in segments for line in segment["lines"]]
    facts, withheld_claims = spoilers.classify_claims(facts, transcript_lines)
    claim_counts = {"claims_" + state: 0 for state in spoilers.REVEAL_STATES}
    for claim in facts:
        if claim.get("reveal_state") == spoilers.REVEAL_LIVE:
            claim_counts["claims_live"] += 1  # companions count here too
    for entry in withheld_claims:
        claim_counts["claims_" + entry["reveal_state"]] += 1

    all_messages: list[dict] = []
    entity_withheld: list[dict] = []
    totals = {"segments": 0, "returned": 0, "anchor_miss": 0, "rejected": 0,
              "resolved": 0, "retries": 0, "reasons": {}, **claim_counts,
              "claims_withheld": len(withheld_claims)}
    used: list[str] = []
    total = len(segments)
    for index, segment in enumerate(segments, start=1):
        prompt = build_prompt(segment, facts, render_segment(segment), used)
        messages: list[dict] = []
        stats: dict = {"returned": 0, "anchor_miss": 0, "rejected": 0, "reasons": {}}
        completed = False
        for attempt in range(2):
            try:
                raw = complete(prompt)
            except Exception:  # noqa: BLE001 - one bad segment must not kill a build
                break
            messages, stats = parse_response(raw, facts, segment)
            completed = not _parse_failed(stats)
            if messages or not _retryable(stats):
                break
            if attempt == 0:
                totals["retries"] += 1
        if completed:
            totals["segments"] += 1
        for key in ("returned", "anchor_miss", "rejected"):
            totals[key] += stats[key]
        totals["resolved"] += stats["returned"] - stats["anchor_miss"]
        for reason, count in stats["reasons"].items():
            totals["reasons"][reason] = totals["reasons"].get(reason, 0) + count
        kept_here = messages[:max_per_segment]
        for message in kept_here:
            used.append(message["text"])
            all_messages.append(message)
        if expander is not None and len(kept_here) < max_per_segment:
            try:
                context_notes = expander.expand(segment, render_segment(segment),
                                                len(kept_here))
            except Exception:  # noqa: BLE001 - Pass B must never sink Pass A
                context_notes = []
            for note in context_notes[:max_per_segment - len(kept_here)]:
                note = spoilers.classify_entity_note(note, transcript_lines,
                                                     title=title)
                if note["reveal_state"] != spoilers.REVEAL_LIVE:
                    # Locked/credits entity notes never reach the page; they
                    # join the credits artifact as grounded entries.
                    entity_withheld.append(spoilers.withheld_note_entry(note))
                    continue
                used.append(note["text"])
                all_messages.append(note)
        if on_segment:
            on_segment(index, total, totals)

    messages = dedupe(all_messages)
    totals["collected"] = len(all_messages)
    totals["after_dedupe"] = len(messages)
    if expander is not None:
        totals["entity"] = dict(expander.stats)
        totals["entity_withheld"] = len(entity_withheld)
    messages = [m for m in messages if not is_bare_credit(m["text"])]
    totals["after_credit_filter"] = len(messages)
    messages = apply_density(messages)
    totals["after_density"] = len(messages)
    # Live is stricter than the aftershow: only sourced notes stream live.
    # Model-only notes move to the credits set, capped at 30% of it, and are
    # labelled credits there (they carry reveal_state "live" from parsing,
    # which described stream eligibility, not artifact membership).
    withheld_notes = [m for m in messages if m["model_only"]]
    for note in withheld_notes:
        note["reveal_state"] = spoilers.REVEAL_CREDITS
        note["unlock_anchor_ms"] = None
    messages = [m for m in messages if not m["model_only"]]
    totals["after_sourced_filter"] = len(messages)
    withheld = spoilers.cap_model_only_notes(withheld_claims + entity_withheld,
                                             withheld_notes)
    totals["withheld"] = len(withheld)
    totals["withheld_model_only"] = sum(1 for e in withheld if e.get("model_only"))
    totals["kept"] = len(messages)
    # Anchor quality is resolved/returned. `kept` is lower because density prunes
    # on purpose; conflating the two makes a good build look broken.
    totals["anchor_hit_rate"] = (
        round(totals["resolved"] / totals["returned"], 3) if totals["returned"] else 0.0
    )
    return messages, totals, withheld
