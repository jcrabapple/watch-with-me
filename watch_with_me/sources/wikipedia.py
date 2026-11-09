"""Wikipedia production notes, fetched as plain text and reduced to facts."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request

API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "WatchWithMe/0.1 (Hermes plugin; https://github.com/jcrabapple)"

PRODUCTION_HINTS = (
    "film", "shot", "filming", "director", "directed", "cast", "casting", "budget",
    "studio", "produced", "production", "screenplay", "script", "animation",
    "animated", "voice", "composer", "score", "released", "premiere", "grossed",
)

# Sections whose sentences are safe to show from minute one of a watch.
# Reception, Critical response and Legacy are excluded on purpose: they
# routinely describe the ending ("critics praised the twist"). Plot and Cast
# duplicate material the sheet already carries, and the appendices are not
# prose. Exclusion is wholesale — an allow-list, not a filter — so no sentence
# from a dropped section can reach a prompt by passing individual checks.
SAFE_SECTIONS = (
    "production", "development", "casting", "filming", "music",
    "release", "box office", "accolades",
)

_HEADING = re.compile(r"^==\s*([^=]+?)\s*==\s*$", re.MULTILINE)

# Plot vocabulary anywhere in a sentence, not just at the start: a production
# sentence that reveals the climax or a death is a spoiler in disguise.
SPOILER_TERMS = (
    "twist", "climax", "ending", "finale", "reveals", "death", "dies", "killed",
    "murderer", "culprit", "identity of",
)


def _http_get(url: str, timeout: float) -> bytes:
    """The single network choke point; every request is counted for cost reports."""
    start = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    finally:
        _STATS["requests"] += 1
        _STATS["seconds"] += time.monotonic() - start


def fetch_stats() -> dict:
    """Request count/elapsed plus article-cache hit/miss counters."""
    return dict(_STATS)


def reset_fetch_stats() -> dict:
    """Zero the counters; returns the stats as they stood (for per-title cost)."""
    previous = dict(_STATS)
    _STATS.update({"requests": 0, "seconds": 0.0, "cache_hits": 0, "cache_misses": 0})
    return previous


_STATS: dict = {"requests": 0, "seconds": 0.0, "cache_hits": 0, "cache_misses": 0}


def fetch_extract(title: str, year: int | None = None, timeout: float = 15.0) -> str:
    query = f"{title} ({year} film)" if year else title
    params = {
        "action": "query", "format": "json", "prop": "extracts",
        "explaintext": "1", "redirects": "1", "titles": query,
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    payload = json.loads(_http_get(url, timeout) or b"{}")
    pages = (payload.get("query") or {}).get("pages") or {}
    for page in pages.values():
        extract = page.get("extract")
        if extract:
            return extract
    return ""


class ArticleCache:
    """On-disk cache of full article extracts, keyed by normalised query.

    One JSON file per query under ``store.data_dir()/article-cache``; a
    rebuild (or a second title mentioning the same person/place) never
    refetches. Entries mirror :func:`fetch_article`'s return value plus the
    query and a timestamp.
    """

    DIRNAME = "article-cache"

    def __init__(self, root=None) -> None:
        if root is None:
            from .. import store
            root = store.data_dir() / self.DIRNAME
        self.root = root

    def _path(self, query: str) -> "object":
        key = re.sub(r"\s+", " ", query.lower()).strip()
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
        return self.root / f"{digest}.json"

    def get(self, query: str) -> dict | None:
        path = self._path(query)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            return None
        if isinstance(entry, dict) and "extract" in entry:
            _STATS["cache_hits"] += 1
            return entry
        return None

    def put(self, query: str, entry: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = dict(entry)
            payload["query"] = query
            payload.setdefault("fetched_at", int(time.time()))
            self._path(query).write_text(
                json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        except OSError:
            pass  # the in-memory path still serves this build
        _STATS["cache_misses"] += 1


def fetch_article(query: str, *, cache: ArticleCache | None = None,
                  timeout: float = 15.0) -> dict:
    """Full plain-text extract for `query`; {"missing": True} when unusable.

    With `cache`, an on-disk hit is returned without a request; a miss is
    fetched once and persisted so rebuilds are free. The entry carries the
    resolved `title`, the canonical `url`, and the `extract`.
    """
    if cache is not None:
        entry = cache.get(query)
        if entry is not None:
            return entry
    params = {
        "action": "query", "format": "json", "prop": "extracts",
        "explaintext": "1", "redirects": "1", "titles": query,
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    payload = json.loads(_http_get(url, timeout) or b"{}")
    entry: dict = {"missing": True}
    pages = (payload.get("query") or {}).get("pages") or {}
    for page in pages.values():
        if page.get("missing"):
            break
        title = page.get("title") or query
        extract = page.get("extract") or ""
        entry = {
            "title": title,
            "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
                title.replace(" ", "_")),
            "extract": extract,
        }
        break
    if cache is not None:
        cache.put(query, entry)
    return entry


def split_sections(extract: str) -> list[tuple[str, str]]:
    """Split an explaintext extract into (heading, body) pairs, in order.

    Text before the first heading is returned under the pseudo-heading "lead".
    Headings keep their original case; callers normalise.
    """
    parts: list[tuple[str, str]] = []
    matches = list(_HEADING.finditer(extract))
    if not matches:
        return [("lead", extract)] if extract.strip() else []
    if matches[0].start() > 0 and extract[:matches[0].start()].strip():
        parts.append(("lead", extract[:matches[0].start()]))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(extract)
        parts.append((match.group(1).strip(), extract[match.end():end]))
    return parts


def _quotes_balanced(text: str) -> bool:
    if text.count('"') % 2:
        return False
    return text.count("\u201c") == text.count("\u201d")


def _wraps_balanced(text: str) -> bool:
    return text.count("(") == text.count(")")


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences without cutting inside quoted speech.

    A naive `(?<=[.!?])\\s+` split breaks at periods inside quotation marks
    ("...still out there." Brian Tallerico...) and leaves shards. Splitting
    only when the quotes opened so far are closed keeps a quoted sentence
    whole; closing quotes stay attached to the sentence they end.
    """
    sentences: list[str] = []
    start = 0
    i, n = 0, len(text)
    while i < n:
        if text[i] in ".!?":
            j = i + 1
            while j < n and text[j] in "\"”’":
                j += 1
            if j >= n or text[j].isspace():
                # Balance is checked on the sentence plus its closing
                # quotes: at "…out there." the closing quote has not been
                # seen yet when the period is reached, so looking only up
                # to the period would report an unbalanced quote and the
                # sentence would never end.
                if not _quotes_balanced(text[start:j]):
                    i += 1
                    continue
                # A period inside a name or domain ("RogerEbert.com",
                # "U.S.") is not a sentence end: the word after it starts
                # lowercase. Treating it as one yields a too-short head
                # and a lowercase-led shard, both rejected downstream.
                look = j
                while look < n and text[look].isspace():
                    look += 1
                if look < n and text[look].islower():
                    i = j
                    continue
                sentences.append(text[start:j])
                start = j
                while start < n and text[start].isspace():
                    start += 1
                i = start
                continue
        i += 1
    if start < n:
        sentences.append(text[start:])
    return [s.strip() for s in sentences if s.strip()]


def section_facts(extract: str, *, min_chars: int = 40, max_chars: int = 300,
                  include_lead: bool = True) -> list[dict]:
    """Spoiler-safe, section-attributed facts from a full article extract.

    Only SAFE_SECTIONS (plus the lead) contribute sentences; Plot, Cast,
    Reception, Critical response, Legacy, References and every other section
    are dropped wholesale. Within a kept section, heading-led fragments,
    quote shards, unbalanced sentences and out-of-band lengths are rejected.
    Each fact carries its section as ``section`` so the fact sheet can label
    it (``wikipedia:production``).
    """
    facts: list[dict] = []
    for heading, body in split_sections(extract):
        key = heading.strip().lower()
        allowed = key in SAFE_SECTIONS or (include_lead and key == "lead")
        if not allowed:
            continue
        for raw in split_sentences(body):
            sentence = " ".join(raw.split())
            if not (min_chars <= len(sentence) <= max_chars):
                continue
            if "==" in sentence:
                continue  # a nested heading leaked into the body
            if sentence.startswith(("…", "...")) or sentence[:1].islower():
                continue  # a shard starting mid-sentence
            if not _quotes_balanced(sentence) or not _wraps_balanced(sentence):
                continue
            lowered = sentence.lower()
            if lowered.startswith(("plot", "the film follows", "the story")):
                continue
            if any(term in lowered for term in SPOILER_TERMS):
                continue
            if key == "lead" and not any(hint in lowered for hint in PRODUCTION_HINTS):
                continue
            facts.append({"text": sentence, "section": key})
    return facts


def production_sentences(extract: str, *, limit: int = 12, max_chars: int = 300) -> list[str]:
    """Plain-text view of the first `limit` spoiler-safe facts (back-compat)."""
    return [f["text"] for f in section_facts(extract, max_chars=max_chars)[:limit]]


def article_url(title: str, year: int | None = None) -> str:
    query = f"{title} ({year} film)" if year else title
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(query.replace(" ", "_"))


# --- rabbit-hole sentence scoring -------------------------------------------
#
# The lead's first usable sentence is usually a definition ("Uganda is a
# landlocked country in East Africa"). The scorer ranks every sentence of an
# article body so fact pickers can choose the most interesting sentence per
# fact, not the first: numbers, years, counts, money and superlatives push a
# sentence up; definitions, "as of", dates-only lines and list tails push it
# down.

_WORD = r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])"

# Strong positive signals, matched against alphanumeric neighbours only so
# hyphenated forms ("fifth-largest") match but subwords ("almost") do not.
_STRONG_TERMS = (
    "first", "only", "largest", "most", "more", "fewer", "oldest",
    "earliest", "biggest", "highest", "longest", "record", "won",
    "nominated", "born in", "grew up", "originally", "best known",
    "best-known", "breakthrough",
)
_STRONG_RE = re.compile("|".join(_WORD.format(re.escape(t)) if " " not in t
                                else re.escape(t) for t in _STRONG_TERMS))
_NUMBER_RE = re.compile(r"\d")
_MONEY_RE = re.compile(_WORD.format(r"(?:million|billion|thousand)") + r"|\$")
_YEAR_RE = re.compile(r"\b(?:1[5-9]\d{2}|20\d{2})\b")
_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}
_DATE_WORDS = _MONTHS | {"born", "age", "aged", "died", "on"}


def _dates_only(lowered: str) -> bool:
    """True when a sentence is nothing but names and dates.

    "( 12 March 1987 )" qualifies; "Uganda gained independence from Britain
    in 1962" does not — "gained" and "independence" are content words.
    """
    if not _YEAR_RE.search(lowered):
        return False
    words = re.findall(r"[a-z]+|\d+", lowered)
    return all(word in _DATE_WORDS or word.isdigit() for word in words)

# A definition sentence: "<Subject> is a/an/the <0-3 modifiers> <noun>", where
# the noun is a class a reader already knows. "is a landlocked country" and
# "is an American actress" both match.
_PLACE_CLASS_NOUNS = (
    "country", "state", "city", "town", "village", "county", "province",
    "region", "island", "nation", "republic", "capital", "settlement",
    "municipality", "territory", "continent", "desert", "river", "mountain",
    "lake", "valley", "peninsula", "archipelago", "borough", "district",
)
_PERSON_CLASS_NOUNS = (
    "actor", "actress", "director", "writer", "screenwriter", "producer",
    "musician", "singer", "rapper", "composer", "author", "novelist",
    "comedian", "playwright", "filmmaker", "artist", "politician",
)
_WORK_CLASS_NOUNS = (
    "film", "movie", "novel", "book", "series", "show", "album", "song",
    "play", "band", "company", "organisation", "organization", "university",
    "museum", "group", "institution",
)
_DEFINITION_RE = re.compile(
    r"\bis (?:an?|the)(?: [\w'-]+){0,3}? (?:"
    + "|".join(_PLACE_CLASS_NOUNS + _PERSON_CLASS_NOUNS + _WORK_CLASS_NOUNS)
    + r")\b", re.IGNORECASE)

# Superlatives that carry no digit but still make a number-shaped claim
# ("has more PhDs per capita than any other state").
_SUPERLATIVE_RE = re.compile(
    _WORD.format("(?:most|more|fewest|fewer|largest|smallest|highest|lowest|"
                 "only|first|last|oldest|newest|biggest|longest|shortest|"
                 "fastest|slowest|best|worst)"))

_CITATION_RE = re.compile(r"\[[^\]]*\]")


def clean_sentence(sentence: str) -> str:
    """Normalise whitespace and strip inline citations/ref markers."""
    text = _CITATION_RE.sub("", sentence)
    return " ".join(text.split())


def _is_definition(lowered: str) -> bool:
    """A definition sentence: "<Subject> is a/an/the <modifiers> <class noun>".

    Modifiers that are themselves rankings ("is the fifth-largest state",
    "is the most populous city") make the sentence a fact, not a definition,
    so a span containing a digit or a superlative does not count.
    """
    match = _DEFINITION_RE.search(lowered)
    if not match:
        return False
    span = match.group(0)
    return not re.search(r"\d", span) and not re.search(r"(?:est|most)\b", span)


def score_sentence(sentence: str) -> int:
    """Interestingness score; negative means actively bad material."""
    text = clean_sentence(sentence)
    lowered = text.lower()
    score = 0
    digits = len(_NUMBER_RE.findall(text))
    if digits:
        score += min(4, 1 + digits // 2)
    if _MONEY_RE.search(lowered):
        score += 1
    score += 2 * min(3, len(_STRONG_RE.findall(lowered)))
    # Hard negatives: no amount of numbers makes these worth showing.
    if _is_definition(lowered):
        return -5
    if "as of" in lowered:
        return -4
    if _dates_only(lowered):
        return -4  # "( 12 March 1987 )" — a dates line, not a fact
    if lowered.startswith(("and ", "but ", "or ")) or text.rstrip().endswith((",", ";")):
        score -= 3  # a list tail, not a sentence
    return score


def _fact_quality_ok(sentence: str, *, min_chars: int, max_chars: int) -> bool:
    if not (min_chars <= len(sentence) <= max_chars):
        return False
    if "==" in sentence:
        return False  # a section heading leaked into the body
    if sentence.startswith(("…", "...")) or sentence[:1].islower():
        return False
    return _quotes_balanced(sentence) and _wraps_balanced(sentence)


def ranked_sentences(extract: str, *, limit: int = 6, min_chars: int = 40,
                     max_chars: int = 300,
                     min_score: int = 1) -> list[str]:
    """The highest-scoring clean sentences of an article, best first.

    Ties keep article order (earlier wins), so a lead fact outranks the same
    score deep in a trivia section. Sentences scoring below `min_score` are
    not interesting enough to offer a note-writer.
    """
    scored: list[tuple[int, int, str]] = []
    for position, raw in enumerate(split_sentences(extract)):
        sentence = clean_sentence(raw)
        if not _fact_quality_ok(sentence, min_chars=min_chars, max_chars=max_chars):
            continue
        value = score_sentence(sentence)
        if value >= min_score:
            scored.append((-value, position, sentence))
    scored.sort()
    return [sentence for _, _, sentence in scored[:limit]]


_PLACE_LEAD_RE = re.compile(
    r"\bis (?:an?|the)(?: [\w'-]+){0,3}? (?:"
    + "|".join(_PLACE_CLASS_NOUNS)
    + r")\b", re.IGNORECASE)


def is_place_article(extract: str) -> bool:
    """True when the article's lead defines the subject as a place."""
    lead = " ".join(split_sentences(extract)[:2])
    return bool(_PLACE_LEAD_RE.search(lead))


def place_fact(extract: str, *, max_chars: int = 240) -> str | None:
    """One superlative or number-bearing fact about a place, or None.

    A place that yields nothing of that shape says nothing rather than
    emitting a definition: the caller must not fall back to the lead.
    """
    best: tuple[int, int, str] | None = None
    for position, raw in enumerate(split_sentences(extract)):
        sentence = clean_sentence(raw)
        if not _fact_quality_ok(sentence, min_chars=40, max_chars=max_chars):
            continue
        lowered = sentence.lower()
        has_number = (bool(_NUMBER_RE.search(sentence))
                      or bool(_MONEY_RE.search(lowered))
                      or bool(_SUPERLATIVE_RE.search(lowered)))
        if not has_number:
            continue  # a place fact must carry a number or superlative
        value = score_sentence(sentence)
        if value < 1:
            continue
        if best is None or value > best[0]:
            best = (value, position, sentence)
    return best[2] if best else None
