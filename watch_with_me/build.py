"""Build a title's timeline: transcript, segments, messages, digests."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cues, spoilers, store, subtitles
from .config import Settings
from .transcript import choose_segments, extract_with_command, parse_srt


@dataclass
class BuildResult:
    rating_key: str
    state: str = "failed"
    transcript_src: str = "none"
    transcript_chars: int = 0
    segments: int = 0
    segmentation: str = "none"
    messages: list[dict] = field(default_factory=list)
    withheld: list[dict] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    error: str | None = None


def sidecar_stream(metadata: dict) -> dict | None:
    for media in metadata.get("Media") or []:
        for part in media.get("Part") or []:
            for stream in part.get("Stream") or []:
                if stream.get("streamType") == 3 and stream.get("key"):
                    return stream
    return None


def media_path(metadata: dict) -> str | None:
    for media in metadata.get("Media") or []:
        for part in media.get("Part") or []:
            if part.get("file"):
                return part["file"]
    return None


def acquire_transcript(metadata: dict, client, settings: Settings,
                       work_dir: Path, conn=None) -> tuple[str, str]:
    """Return (srt_text, source). Source is cache|sidecar|extractor|none.

    With ``conn`` supplied, the plugin's own subtitle cache is asked first:
    downloaded sidecars can vanish from Plex when the user changes the
    selected track (RESEARCH-PREPARATION.md §4.3), so a cache hit is the
    durable path and Plex is never re-read for a cached transcript.
    """
    if conn is not None:
        rating_key = str(metadata.get("ratingKey") or "")
        hit = subtitles.cached_transcript(conn, rating_key,
                                          settings.subtitle_languages or ["en"])
        if hit is not None:
            return hit[0], "cache"

    stream = sidecar_stream(metadata)
    if stream is not None:
        try:
            text = client.sidecar(stream["key"], stream.get("codec") or "srt")
            if text.strip():
                # The cached copy is the durable artifact (subtitles.py
                # contract): a sidecar Plex later removes must not take the
                # transcript with it. Projectionist asks read this cache.
                if conn is not None:
                    language = stream.get("languageCode") or "en"
                    for preference in (settings.subtitle_languages or ["en"]):
                        if subtitles.language_matches(preference, language):
                            language = preference
                            break
                    subtitles.write_cache(conn, rating_key, language, text)
                return text, "sidecar"
        except Exception:  # noqa: BLE001 - fall through to the extractor
            pass

    path = media_path(metadata)
    if settings.extract_command and path:
        output = work_dir / f"{metadata.get('ratingKey', 'item')}.srt"
        stream_id = str(stream.get("id", "")) if stream else ""
        if extract_with_command(settings.extract_command, media_path=path,
                                stream_id=stream_id, output=output):
            return output.read_text("utf-8", "replace"), "extractor"
    return "", "none"


def build_messages(metadata: dict, srt_text: str, wikipedia_sentences: list[str],
                   complete, *, max_segments: int | None = None,
                   person_facts: list[str] | None = None,
                   on_segment=None, expander=None) -> BuildResult:
    result = BuildResult(rating_key=str(metadata.get("ratingKey") or ""),
                         transcript_chars=len(srt_text))
    if not srt_text.strip():
        result.error = "no_transcript"
        result.state = "degraded"
        return result

    parsed = parse_srt(srt_text)
    if not parsed:
        result.error = "unparsable_transcript"
        result.state = "degraded"
        return result

    segments, method = choose_segments(metadata, parsed)
    result.segments = len(segments)
    result.segmentation = method
    if not segments:
        result.error = "no_segments"
        result.state = "degraded"
        return result
    if max_segments:
        segments = segments[:max_segments]

    facts = cues.build_fact_sheet(metadata, wikipedia_sentences, person_facts=person_facts)
    messages, stats, withheld = cues.generate(segments, facts, complete,
                                              on_segment=on_segment, expander=expander,
                                              transcript_lines=parsed,
                                              title=metadata.get("title"))
    result.messages = messages
    result.withheld = withheld
    # Re-run the same deterministic classification generate applied, to keep
    # the full claim ledger (live rows included) for runtime queries — the
    # Projectionist asks against claims_unlocked_at, not just the artifact.
    classified, credits_withheld = spoilers.classify_claims(facts, parsed)
    seen_ids = {c["id"] for c in classified}
    result.claims = classified + [dict(e, claim_id=e["claim_id"])
                                  for e in credits_withheld
                                  if e["reveal_state"] == "credits"
                                  and e["claim_id"] not in seen_ids] \
        + [n for n in withheld if n.get("model_only")]
    result.stats = stats
    result.state = "ready" if messages else "degraded"
    if not messages:
        result.error = "no_messages_passed_validation"
    return result


def persist(conn, result: BuildResult, metadata: dict, build_version: int = 1) -> None:
    rating_key = result.rating_key
    with conn:
        conn.execute(
            "INSERT INTO item(rating_key,media_type,title,year,runtime_ms,view_count,"
            "transcript_src,build_state,build_version,built_at,error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(rating_key) DO UPDATE SET "
            "title=excluded.title, year=excluded.year, runtime_ms=excluded.runtime_ms, "
            "transcript_src=excluded.transcript_src, build_state=excluded.build_state, "
            "build_version=excluded.build_version, built_at=excluded.built_at, "
            "error=excluded.error",
            (
                rating_key,
                metadata.get("type") or "movie",
                metadata.get("title") or "",
                metadata.get("year"),
                int(metadata.get("duration") or 0),
                int(metadata.get("viewCount") or 0),
                result.transcript_src,
                result.state,
                build_version,
                int(time.time()),
                result.error,
            ),
        )
        conn.execute("DELETE FROM message WHERE rating_key=?", (rating_key,))
        for message in result.messages:
            conn.execute(
                "INSERT INTO message(id,rating_key,t_ms,kind,text,thumb_url,source_name,"
                "source_url,model_only,confidence,anchor,build_version,"
                "reveal_state,unlock_anchor_ms,subject_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message["id"], rating_key, message["t_ms"], message["kind"],
                    message["text"], None, message.get("source_name"),
                    message.get("source_url"), message.get("model_only", 0),
                    message.get("confidence"), message.get("anchor"), build_version,
                    message.get("reveal_state", "live"),
                    message.get("unlock_anchor_ms"), message.get("subject_key"),
                ),
            )
        store.persist_claims(conn, rating_key, result.claims,
                             build_version=build_version)
