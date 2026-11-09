"""Per-person facts for the fact sheet: who a credited actor is, briefly.

The film's own Wikipedia page is thin for episodes, and the model cannot invent
an actor's filmography without guessing. Plex hands us the cast; Wikipedia's
article tells us what each of them is known for. Those sentences become
fact-sheet lines, so a filmography note is grounded rather than plausible.

Rabbit hole: instead of one lead sentence per actor, up to three distinct
facts are extracted from the full article — a notable role (with a year when
the article gives one), an award or nomination, and a biography oddity
(how they started, where they grew up, a first). Each line is tagged with
the actor's article as its source. Facts the article does not state plainly
are skipped: no inference, no padding.
"""

from __future__ import annotations

import re

from . import wikipedia

CUE_WORDS = (
    "known for", "best known", "starred", "starring", "appeared", "played",
    "won ", "nominated", "award", "lead role", "featured", "career",
)
SKIP_HINTS = ("plot", "fictional character", "storyline", "episode of")

MAX_FACTS_PER_PERSON = 3
MIN_FACT_CHARS = 40
MAX_FACT_CHARS = 420  # multi-role "known for" sentences run long; the model
                      # condenses them into a note, citing this line

# Fact slots, in priority order. A sentence fills the first slot it matches
# and each sentence may fill only one slot, so a single "best known for …
# a role that won her an Emmy" sentence never becomes two facts.
_ROLE_RE = re.compile(
    r"\b(?:played|plays|starred|portrayed|voiced|directed|directs|"
    r"known for|best known)\b",
    re.IGNORECASE)
_AWARD_RE = re.compile(r"\b(?:won|winner|nominated|nomination|award)\b",
                       re.IGNORECASE)
_BIO_RE = re.compile(
    r"\b(?:born in|grew up|raised in|originally|first job|debut|"
    r"began (?:his|her|their) career|started (?:his|her|their) career|"
    r"drop(?:ped)? out)\b",
    re.IGNORECASE)
_YEAR_RE = re.compile(r"\((?:19|20)\d{2}\)|\b(?:19|20)\d{2}\b")

_SLOTS = (
    ("role", _ROLE_RE),
    ("award", _AWARD_RE),
    ("bio", _BIO_RE),
)


def _sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text or "").strip()
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", flat) if s.strip()]


def lead_sentence(extract: str, name: str, *, max_chars: int = 280) -> str | None:
    """The most useful biographical sentence from an article lead.

    Prefer a sentence that says what the person is known for; fall back to the
    first usable sentence. Plot or in-universe text is never useful here.
    """
    surname = name.split()[-1].lower() if name.split() else ""
    candidates = []
    for sentence in _sentences(extract)[:12]:
        if not (40 <= len(sentence) <= max_chars):
            continue
        lowered = sentence.lower()
        if any(hint in lowered for hint in SKIP_HINTS):
            continue
        candidates.append((sentence, lowered))
    if not candidates:
        return None
    for sentence, lowered in candidates:
        if surname and surname in lowered and any(cue in lowered for cue in CUE_WORDS):
            return sentence
    for sentence, lowered in candidates:
        if any(cue in lowered for cue in CUE_WORDS):
            return sentence
    return candidates[0][0]


def _mentions_any(text: str, keys: list[str]) -> bool:
    for key in keys:
        if key and re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text):
            return True
    return False


def _clean_candidates(extract: str, *, title_keys: list[str]) -> list[str]:
    """Usable article sentences for person facts, in article order.

    The watched title's name never appears: an actor's article can describe
    this film's plot, and a person fact must never leak it. The spoiler
    vocabulary filter from the production extractor applies here too.
    """
    out: list[str] = []
    for raw in wikipedia.split_sentences(extract):
        sentence = wikipedia.clean_sentence(raw)
        if not (MIN_FACT_CHARS <= len(sentence) <= MAX_FACT_CHARS):
            continue
        if sentence[:1].islower() or sentence.startswith(("…", "...")):
            continue
        lowered = sentence.lower()
        if any(term in lowered for term in wikipedia.SPOILER_TERMS):
            continue
        if any(hint in lowered for hint in SKIP_HINTS):
            continue
        if title_keys and _mentions_any(lowered, title_keys):
            continue
        out.append(sentence)
    return out


def person_facts(extract: str, name: str, *, title_keys: list[str] | None = None,
                 limit: int = MAX_FACTS_PER_PERSON) -> list[str]:
    """Up to `limit` distinct facts about `name` from their full article.

    Slot order: a notable role (a year raises its score), an award or
    nomination, then a biography oddity. Within a slot the highest-scoring
    sentence wins; a sentence already used by an earlier slot is skipped, and
    facts the article does not state plainly are omitted rather than inferred.
    """
    candidates = _clean_candidates(extract, title_keys=title_keys or [])
    used: set[str] = set()
    facts: list[str] = []
    for slot, pattern in _SLOTS:
        if len(facts) >= limit:
            break
        best: tuple[int, int, str] | None = None
        for position, sentence in enumerate(candidates):
            if sentence in used or not pattern.search(sentence):
                continue
            score = wikipedia.score_sentence(sentence)
            if slot == "role":
                if _YEAR_RE.search(sentence):
                    score += 2  # a dated role is the strongest filmography fact
                if re.search(r"\b(?:known for|best known)\b", sentence,
                             re.IGNORECASE):
                    score += 2  # the article itself flags the notable role
            if score < 0:
                continue
            if best is None or score > best[0]:
                best = (score, position, sentence)
        if best is None:
            continue
        used.add(best[2])
        facts.append(best[2])
    return facts


def actor_facts(metadata: dict, fetch, *, limit: int = 8) -> list[dict]:
    """Fact-sheet entries: {'text', 'source_name', 'url'} per credited actor.

    `fetch(name)` may return an article extract string or a dict with
    `extract`/`url`/`title` (the ArticleCache entry shape); anything else is
    skipped. Failures are skipped: a thin fact sheet is better than a build
    that dies. The first fact keeps the old "Name plays Role." prefix when it
    does not already name the actor; later facts stand alone, attributed to
    the actor's article in the sheet.
    """
    title_keys = [
        " ".join(re.sub(r"[^\w\s]", " ", (metadata.get(key) or "").lower()).split())
        for key in ("title", "grandparentTitle")
        if metadata.get(key)
    ]
    lines: list[dict] = []
    for role in (metadata.get("Role") or [])[:limit]:
        name = (role.get("tag") or "").strip()
        if not name:
            continue
        try:
            fetched = fetch(name)
        except Exception:  # noqa: BLE001 - a missing article is not an error
            continue
        if isinstance(fetched, dict):
            extract = fetched.get("extract") or ""
            url = fetched.get("url") or ""
        else:
            extract, url = fetched or "", ""
        facts = person_facts(extract, name, title_keys=title_keys)
        if not facts:
            continue
        part = (role.get("role") or "").strip()
        source = (f"wikipedia:{fetched['title']}"
                  if isinstance(fetched, dict) and fetched.get("title")
                  else f"wikipedia:{name}")
        first = facts[0]
        if not first.lower().startswith(name.lower()):
            prefix = f"{name} plays {part}." if part else f"{name}."
            first = f"{prefix} {first}"
        for index, fact in enumerate(facts):
            lines.append({"text": first if index == 0 else fact,
                          "source_name": source, "url": url or None})
    return lines
