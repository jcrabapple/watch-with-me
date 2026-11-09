"""Pass B: entity expansion — adjacent facts about things dialogue mentions.

Pass A notes come from the title-scoped fact sheet. When a segment mentions a
real place, person, object, event or work that has its own Wikipedia article,
this pass may add one ``context`` note about that entity, anchored to the line
that mentions it. The note is grounded in the entity's Wikipedia summary, so
it lands with ``model_only=0`` and a real ``source_url`` instead of being
unverified model knowledge.

Per segment the cost is at most two extra model calls (entity extraction, then
note writing) and two Wikipedia fetches, and only when Pass A left room (a
segment that already produced two Pass A notes is skipped). Lookups are cached
by normalised name both in memory (per build) and on disk (under
``store.data_dir()``), so a rewatch and a second title do not refetch.

Wikipedia is the only source here, by design: the excluded providers
(TMDB/OMDb/Trakt/Letterboxd/IMDb) were excluded on their own terms by the
research and never enter the note pipeline.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse  # noqa: F401 - tests monkeypatch entities.urllib.request
import urllib.request  # noqa: F401 - tests monkeypatch entities.urllib.request
from typing import Callable

from . import cues, store

MAX_ENTITIES = 2          # the extraction call may name at most two
MAX_CANDIDATES = 5        # with a Jev selector: it chooses among up to five
MAX_FETCHES = 2           # per segment
MAX_NOTES = 2             # per segment, inside the 3-note segment budget
MIN_SUMMARY_CHARS = 60    # below this the article is a stub for our purposes
SUMMARY_SENTENCES = 3
MAX_PER_SEGMENT = 3       # skip Pass B only when the segment is already full

DISAMBIGUATION_MARKERS = ("may refer to", "may also refer to", "can refer to")

# A generic noun is never an entity worth a lookup. Pronouns and the common
# nouns below are rejected outright; other single all-lowercase words are
# treated the same way in is_generic. Multi-word phrases survive to the
# fetch, which turns a bad name into a cached negative.
_GENERIC_WORDS = {
    "mom", "dad", "car", "gun", "house", "city", "town", "money", "phone",
    "book", "song", "movie", "dog", "church", "school", "baby", "wine",
}

ENTITY_RULES = """You identify real-world entities mentioned in a transcript segment.
Output JSON only, exactly this shape:
  {"entities": [{"name": "...", "why": "one clause"}]}
Rules:
- At most 2 entities.
- A named real place, person, object, event or creative work that has its own
  Wikipedia article.
- "why" is one short clause: why a viewer might want to know about it right now.
- Reject pronouns and generic nouns (mom, car, the city).
- Reject character names and anyone in CAST AND CREW.
- Reject anything in ALREADY EXPANDED.
- Zero entities is a correct answer when nothing qualifies.
"""

# Jev-on variant: identical except the wider candidate budget. The selector
# (watch_with_me.jev) chooses among the named candidates before any fetch;
# only the chosen ones are offered for fetching. Kept byte-identical to
# ENTITY_RULES everywhere else so both prompts share every other rule.
ENTITY_RULES_JEV = ENTITY_RULES.replace(
    "At most 2 entities.", f"At most {MAX_CANDIDATES} entities.")

NOTE_RULES = """You write one short trivia note about a real-world entity mentioned
in this film or episode segment.
Rules:
- Output JSON only, exactly this shape:
  {"messages": [{"anchor": "a line copied from the transcript", "text": "One or two sentences.", "confidence": 0.9}]}
- At most one message. {"messages": []} is correct when the facts have no
  usable interesting sentence.
- The fact describes the entity only. It must not claim the film references,
  was inspired by, or is connected to the entity.
- No "echoes", "resembles", or "is a nod to" comparisons. State what is known;
  do not assert a connection the source does not make.
- Use only the WIKIPEDIA FACTS. Never add your own knowledge.
- Nothing about plot developments after the segment end. You have not been shown them.
- "anchor" must be a line from the transcript that mentions the entity, copied
  verbatim, 120 characters or fewer.
- "text" is one or two sentences, at most 240 characters.
- "confidence" is required, a number from 0 to 1.
"""


def normalise_name(name: str) -> str:
    """Cache/exclusion key for an entity name: lowercase, alnum collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", name.lower())).strip()


def is_generic(name: str) -> bool:
    """True for pronouns and bare generic nouns; never worth a lookup.

    Single all-lowercase words are common nouns the model should not have
    returned. Multi-word phrases stay: real titles are sometimes stylized
    lowercase ("mojo bag", "the outsider"), and a failed lookup just becomes
    a cached negative instead of a wrong note.
    """
    flat = normalise_name(name)
    if not flat or flat in _GENERIC_WORDS:
        return True
    if len(flat) <= 3 and flat in {"he", "she", "him", "her", "it", "they", "them"}:
        return True
    words = flat.split()
    if len(words) == 1 and not any(ch.isupper() or ch.isdigit() for ch in name):
        return True
    return False


def cast_names(metadata: dict) -> set[str]:
    """Normalised cast, character and crew names: never expansion targets."""
    out: set[str] = set()
    for role in metadata.get("Role") or []:
        for value in (role.get("tag"), role.get("role")):
            key = normalise_name(value or "")
            if key:
                out.add(key)
    for field in ("Director", "Writer", "Producer"):
        for person in metadata.get(field) or []:
            key = normalise_name(person.get("tag") or "")
            if key:
                out.add(key)
    return out


def parse_entities(raw: str, *, limit: int = MAX_ENTITIES) -> list[dict]:
    """Validate the extraction call's response: strict JSON, clean names."""
    try:
        payload = json.loads(cues._extract_json(raw))  # noqa: SLF001 - shared JSON carving
    except ValueError:
        return []
    items = payload.get("entities") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or len(out) >= limit:
            continue
        name = " ".join(str(item.get("name") or "").split())
        why = " ".join(str(item.get("why") or "").split())
        key = normalise_name(name)
        if not name or not key or key in seen or is_generic(name):
            continue
        seen.add(key)
        out.append({"name": name, "why": why[:120]})
    return out


def usable_summary(text: str) -> bool:
    """False for stubs, disambiguation pages and empty extracts."""
    flat = " ".join((text or "").split())
    if len(flat) < MIN_SUMMARY_CHARS:
        return False
    lowered = flat.lower()
    return not any(marker in lowered for marker in DISAMBIGUATION_MARKERS)


def head_sentences(text: str, limit: int = SUMMARY_SENTENCES) -> str:
    """The first `limit` sentences of an explaintext lead."""
    from .sources.wikipedia import split_sentences

    return " ".join(split_sentences(text)[:limit])


def fetch_summary(name: str, *, timeout: float = 15.0) -> dict:
    """Wikipedia article for `name`; {"missing": True} when unusable.

    One API call per name, redirected, fetching the full extract (not just
    the intro): the lead decides usability and place-ness, while the body
    supplies superlative/number facts the intro never carries. Entries cache
    the extract on disk so a rebuild never refetches.
    """
    from .sources import wikipedia

    try:
        article = wikipedia.fetch_article(name, timeout=timeout)
    except Exception:  # noqa: BLE001 - a lookup failure is a negative result
        return {"missing": True}
    if article.get("missing"):
        return {"missing": True}
    extract = article.get("extract") or ""
    summary = head_sentences(extract)
    if not usable_summary(summary):
        return {"missing": True}
    place = wikipedia.is_place_article(extract)
    return {"name": article["title"], "url": article["url"], "summary": summary,
            "extract": extract, "place": place,
            "place_fact": wikipedia.place_fact(extract) if place else None,
            "fetched_at": int(time.time())}


class EntityCache:
    """Normalised-name cache, in memory and on disk under store.data_dir().

    Entries are either a summary dict (``name``, ``url``, ``summary``) or
    ``{"missing": True}``; both are cached so disambiguation pages and stubs
    are never refetched either. The file is small JSON, rewritten on each put.
    """

    FILENAME = "entity-cache.json"

    def __init__(self, path=None) -> None:
        if path is None:
            path = store.data_dir() / self.FILENAME
        self.path = path
        self._entries: dict[str, dict] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text("utf-8"))
                if isinstance(raw, dict):
                    self._entries = {k: v for k, v in raw.items()
                                     if isinstance(v, dict)}
            except (ValueError, OSError):
                self._entries = {}

    def get(self, name: str) -> dict | None:
        return self._entries.get(normalise_name(name))

    def put(self, name: str, entry: dict) -> None:
        self._entries[normalise_name(name)] = entry
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._entries, indent=1,
                                            sort_keys=True), encoding="utf-8")
        except OSError:
            pass  # the in-memory half still serves this build

    def __len__(self) -> int:
        return len(self._entries)


class Expander:
    """Per-segment Pass B runner. One instance per build.

    `complete(prompt) -> str` is the same injected model call Pass A uses.
    `excluded` is the normalised cast/character/crew-name set; anything the
    model returns for one of those is dropped in code, not just in the prompt.
    `selector(candidates, segment) -> list | None`, when given, is the
    optional Jev entity-selection step: it runs after candidates are named
    and before any fetch. A list return (possibly []) replaces the candidate
    set ([] suppresses the segment); None means 'unusable or ambiguous — use
    the existing heuristic'. When it is None (Jev off, unkeyed, or
    unreachable), behaviour is byte-identical to a plain Expander.
    """

    def __init__(self, complete, *, cache: EntityCache | None = None,
                 excluded: set[str] | None = None,
                 selector: Callable | None = None) -> None:
        self.complete = complete
        # `cache if cache is not None` on purpose: an empty EntityCache is
        # falsy (__len__), so `cache or EntityCache()` would silently swap a
        # fresh explicit cache for the shared on-disk one.
        self.cache = cache if cache is not None else EntityCache()
        self.excluded = excluded or set()
        self.selector = selector
        self.expanded: set[str] = set()  # normalised names tried this build
        self.stats: dict = {"calls": 0, "fetches": 0, "notes": 0,
                            "skipped_names": 0, "unusable": 0}
        if selector is not None:
            # Only with a selector does the shape change: a Jev-off build's
            # stats stay byte-identical to the pre-Jev ones.
            self.stats.update({"jev_asked": 0, "jev_chosen": 0, "jev_none": 0,
                               "jev_fallback": 0})

    def expand(self, segment: dict, rendered: str, pass_a_count: int) -> list[dict]:
        """Up to two context notes for this segment; [] on any failure."""
        if pass_a_count >= MAX_PER_SEGMENT:
            return []
        rules = ENTITY_RULES_JEV if self.selector is not None else ENTITY_RULES
        limit = MAX_CANDIDATES if self.selector is not None else MAX_ENTITIES
        prompt = (
            f"{rules}\n"
            f"CAST AND CREW (never return these): {', '.join(sorted(self.excluded)) or '(none)'}\n"
            f"ALREADY EXPANDED THIS BUILD (never return these): "
            f"{', '.join(sorted(self.expanded)) or '(none)'}\n"
            f"SEGMENT: {segment['start_ms'] // 1000}s to {segment['end_ms'] // 1000}s\n"
            f"TRANSCRIPT\n{rendered}\n"
        )
        try:
            raw = self.complete(prompt)
        except Exception:  # noqa: BLE001 - one bad segment must not kill a build
            return []
        self.stats["calls"] += 1
        entities = parse_entities(raw, limit=limit)

        if self.selector is not None and entities:
            self.stats["jev_asked"] += 1
            try:
                picked = self.selector(entities, segment)
            except Exception:  # noqa: BLE001 - selection must never sink a build
                picked = None
            if picked is None:
                # Unreachable, HTTP error, malformed answer, contract
                # violation, or the ambiguous band: the existing heuristic
                # decides over the same candidates.
                self.stats["jev_fallback"] += 1
            else:
                self.stats["jev_none"] += 0 if picked else 1
                self.stats["jev_chosen"] += len(picked)
                entities = picked

        fetches = 0
        notes: list[dict] = []
        for entity in entities:
            if fetches >= MAX_FETCHES or len(notes) >= MAX_NOTES:
                break
            key = normalise_name(entity["name"])
            if key in self.excluded or key in self.expanded:
                self.stats["skipped_names"] += 1
                continue
            entry = self.cache.get(entity["name"])
            # Pre-rabbit-hole cache entries carry only the intro summary;
            # refetch once to upgrade them to full extracts.
            stale = entry is not None and "extract" not in entry \
                and "missing" not in entry
            if entry is None or stale:
                if fetches >= MAX_FETCHES:
                    break
                fetches += 1
                self.stats["fetches"] += 1
                entry = fetch_summary(entity["name"])
                self.cache.put(entity["name"], entry)
            self.expanded.add(key)
            if entry.get("missing") or not entry.get("summary"):
                self.stats["unusable"] += 1
                continue
            if entry.get("place") and entry.get("place_fact"):
                note = self._place_note(segment, entity, entry)
            else:
                note = self._write_note(segment, rendered, entity, entry)
            if note is not None:
                self.stats["notes"] += 1
                notes.append(note)
        return notes

    def _place_note(self, segment: dict, entity: dict, entry: dict) -> dict | None:
        """A deterministic note from the article's best place fact.

        Place articles never need a model rewrite: the scorer already picked
        the single most interesting number-bearing sentence. The anchor is
        the transcript line that mentions the entity; when no line contains
        the full name verbatim, fuzzy anchor resolution decides.
        """
        text = " ".join(str(entry.get("place_fact") or "").split())
        if not text or len(text) > cues.MAX_TEXT:
            return None
        key = normalise_name(entity["name"])
        anchor = t_ms = ratio = None
        for line in segment["lines"]:
            if key and key in normalise_name(line.get("text") or ""):
                anchor = (line.get("text") or "")[:cues.MAX_ANCHOR]
                t_ms = int(line["start_ms"])
                ratio = 1.0
                break
        if anchor is None:
            resolved = cues.resolve_anchor(entity["name"], segment["lines"])
            if resolved is None:
                return None
            anchor, t_ms, ratio = entity["name"], resolved["t_ms"], resolved["ratio"]
        digest = hashlib.sha1(
            f"ctx:{segment['start_ms']}:{anchor}:{text}".encode()).hexdigest()[:12]
        return {
            "id": f"msg_{digest}",
            "t_ms": t_ms,
            "kind": "context",
            "text": text,
            "anchor": anchor,
            "anchor_ratio": ratio,
            "fact_ids": [],
            "model_only": 0,  # grounded in the entity article, not model-only
            "source_name": f"wikipedia:{entry.get('name') or entity['name']}",
            "source_url": entry.get("url"),
            "confidence": 0.85,
            "subject": normalise_name(entity["name"])[:80],
        }

    def _write_note(self, segment: dict, rendered: str, entity: dict,
                    entry: dict) -> dict | None:
        from .sources import wikipedia

        ranked = wikipedia.ranked_sentences(entry.get("extract") or "")
        material = ranked if ranked else [entry.get("summary") or ""]
        facts_block = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(material))
        prompt = (
            f"{NOTE_RULES}\n"
            f"ENTITY: {entity['name']}"
            + (f" — {entity['why']}" if entity.get("why") else "") + "\n"
            f"WIKIPEDIA FACTS (your only source), best first\n{facts_block}\n\n"
            f"SEGMENT: {segment['start_ms'] // 1000}s to {segment['end_ms'] // 1000}s\n"
            f"TRANSCRIPT\n{rendered}\n"
        )
        try:
            raw = self.complete(prompt)
        except Exception:  # noqa: BLE001
            return None
        self.stats["calls"] += 1
        try:
            payload = json.loads(cues._extract_json(raw))  # noqa: SLF001
        except ValueError:
            return None
        items = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            return None
        item = items[0]
        if not isinstance(item, dict):
            return None
        anchor = str(item.get("anchor") or "").strip()
        text = " ".join(str(item.get("text") or "").split())
        if not anchor or not text or len(anchor) > cues.MAX_ANCHOR \
                or len(text) > cues.MAX_TEXT:
            return None
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < cues.MIN_CONFIDENCE:
            return None
        resolved = cues.resolve_anchor(anchor, segment["lines"])
        if resolved is None:
            return None
        digest = hashlib.sha1(
            f"ctx:{segment['start_ms']}:{anchor}:{text}".encode()).hexdigest()[:12]
        return {
            "id": f"msg_{digest}",
            "t_ms": resolved["t_ms"],
            "kind": "context",
            "text": text,
            "anchor": anchor,
            "anchor_ratio": resolved["ratio"],
            "fact_ids": [],
            "model_only": 0,  # grounded in the entity article, not model-only
            "source_name": f"wikipedia:{entry.get('name') or entity['name']}",
            "source_url": entry.get("url"),
            "confidence": confidence,
            "subject": normalise_name(entity["name"])[:80],
        }
