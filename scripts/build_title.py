#!/usr/bin/env python3
"""Build one title's timeline from a real Plex item, with a real model.

    export PLEX_TOKEN=...
    export PLEX_BASE_URL=http://<server>:32400
    python scripts/build_title.py --rating-key 65481 --out artifacts/cycle.json

Reads the sidecar subtitle (or runs --extract-command), pulls Wikipedia
production sentences, calls the model once per segment, and prints the anchor
hit rate plus the generated messages. This is the Phase 2 quality gate.

The model is resolved, never hardcoded (see watch_with_me.models):
``--model`` if given, else the plugin ``note_model`` override (only when the
host allows model overrides), else ``auxiliary.watch_with_me_cues`` in the
Hermes config, else its ``compression`` inherit, else the ``WWM_MODEL`` /
``OPENROUTER_MODEL`` environment variables. Entity expansion (per-entity cast
facts) can be turned off with ``--no-entity-expansion``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watch_with_me import build, cues  # noqa: E402
from watch_with_me import entities, models  # noqa: E402
from watch_with_me.config import Settings  # noqa: E402
from watch_with_me.plex import PlexClient  # noqa: E402
from watch_with_me.sources import cast, wikipedia  # noqa: E402

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"


def _api_key() -> str:
    """The OpenRouter key from the process environment, and nothing else.

    This script can be run inside an installed tree where one host serves
    several profiles, so it never reads ``~/.hermes/.env`` (that file carries
    the launch profile's key) and never guesses.
    """
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key:
        return key
    raise SystemExit("OPENROUTER_API_KEY is not set")


def make_completer(model: str, reasoning: str = "low"):
    key = _api_key()

    def call(prompt: str, reasoning_mode: str) -> tuple[str, dict]:
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 2500,
            "response_format": {"type": "json_object"},
        }
        if reasoning_mode == "off":
            payload["reasoning"] = {"enabled": False}
        elif reasoning_mode != "default":
            payload["reasoning"] = {"effort": reasoning_mode}
        request = urllib.request.Request(
            OPENROUTER, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and reasoning_mode != "default":
                # Some routes reject the reasoning parameter outright.
                print(f"[model] reasoning={reasoning_mode} rejected; retrying without it",
                      file=sys.stderr, flush=True)
                return call(prompt, "default")
            raise
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = (message.get("content") or "").strip()
        detail = {
            "finish_reason": choice.get("finish_reason"),
            "usage": body.get("usage") or {},
            "keys": sorted(message),
        }
        if not content:
            detail["reasoning_body"] = (message.get("reasoning_content")
                                        or message.get("reasoning") or "")
        return content, detail

    def complete(prompt: str) -> str:
        content, detail = call(prompt, reasoning)
        if not content:
            think_tokens = ((detail["usage"].get("completion_tokens_details") or {})
                            .get("reasoning_tokens") or 0)
            if think_tokens:
                # The budget went to deliberation. Ask again with it switched off.
                content, detail = call(prompt, "off")
        if not content:
            print(f"[model] empty content finish_reason={detail['finish_reason']} "
                  f"usage={detail['usage']} keys={detail['keys']}",
                  file=sys.stderr, flush=True)
        return content or detail.get("reasoning_body", "")

    return complete


def with_debug(complete, directory: Path):
    """Wrap a completer so every prompt and raw response lands on disk."""
    import itertools

    directory.mkdir(parents=True, exist_ok=True)
    counter = itertools.count(1)

    def wrapped(prompt: str) -> str:
        number = next(counter)
        (directory / f"{number:03d}-prompt.txt").write_text(prompt, encoding="utf-8")
        raw = complete(prompt)
        (directory / f"{number:03d}-response.txt").write_text(raw or "", encoding="utf-8")
        return raw

    return wrapped


def add_model_arguments(parser: argparse.ArgumentParser) -> None:
    """Model-selection flags; defaults come from the resolver, not constants."""
    parser.add_argument("--model", default="",
                        help="note model id; defaults to the resolved model "
                             "(picker, override, or WWM_MODEL/OPENROUTER_MODEL)")
    parser.add_argument("--reasoning", default="",
                        choices=["", "off", "low", "medium", "high", "default"],
                        help="reasoning effort; defaults to the note_effort setting (low)")


def load_settings(config: dict | None = None) -> Settings:
    """Plugin settings from the Hermes config, if one is reachable."""
    if config is None:
        config = models.load_hermes_config()
    return Settings.from_dict(models.plugin_settings(config))


def pick_model(settings: Settings, cli_model: str, *, env=None, config=None) -> tuple[str, models.ModelChoice]:
    """Resolve the note model for this build, warning when an override is refused."""
    choice = models.resolve_model(settings, env=env, config=config, standalone=True)
    if settings.note_model and not choice.override_allowed:
        print(f"[model] note_model={settings.note_model!r} ignored: the host has not allowed "
              f"model overrides (plugins.entries.watch-with-me.llm.allow_model_override)",
              file=sys.stderr, flush=True)
    if cli_model:
        return cli_model, choice
    if not choice.model:
        raise SystemExit(models.model_unset_message())
    return choice.model, choice


def effective_reasoning(cli_value: str, settings: Settings) -> str:
    return cli_value or settings.note_effort or "low"


def fetch_person_facts(metadata: dict, enabled: bool, fetch_article_fn) -> list[dict]:
    """Per-actor fact-sheet entries; entity_expansion=False leaves the build path unchanged."""
    if not enabled:
        return []
    return cast.actor_facts(metadata, fetch_article_fn)


def build_expander(settings: Settings, complete, metadata: dict,
                   *, wikipedia: bool) -> entities.Expander | None:
    """Pass B runner, or None when the settings flag (or source) is off.

    Returning None — rather than a disabled expander — keeps the Pass A call
    sequence and prompts byte-identical, which the cost-guard test pins down.
    """
    if not settings.entity_expansion or not wikipedia:
        return None
    # Strictly optional: None when jev_enabled is off or no key resolves —
    # the Expander then behaves exactly as it did before Jev existed.
    from watch_with_me import jev

    selector = jev.make_selector(settings)
    return entities.Expander(complete,
                             cache=entities.EntityCache(),
                             excluded=entities.cast_names(metadata),
                             selector=selector)


def wikipedia_title(metadata: dict) -> str:
    """The title Wikipedia is queried for: series name for episodes, else the title.

    Plex names an episode by its own title ("A Bad Lie") with the show in
    `grandparentTitle` ("Elsbeth"); searching the episode title as a film 404s.
    """
    if metadata.get("type") == "episode":
        return metadata.get("grandparentTitle") or metadata.get("title") or ""
    return metadata.get("title") or ""


def fetch_article_extract(metadata: dict, fetch=None) -> tuple[str, str, int | None]:
    """The Wikipedia extract for this item plus the query that produced it.

    Returns (extract, query_title, query_year) so the caller can build the
    matching article URL from the *resolved* query, not the raw title.

    Episodes need two guards (measured in artifacts/phase6/ as lever 2, on
    9-1-1 S10E1, ratingKey 65130):

    - The episode's year must not be passed: fetch_extract(title, year)
      appends "(year film)", so an episode queried "9-1-1 (2026 film)"
      misses every series article (0 chars returned).
    - A bare series name can be a different primary topic: "9-1-1" resolves
      to the emergency telephone number (13,877 chars, 0 safe facts — the
      sections are History/Funding/Problems). The disambiguated
      "(TV series)" form is tried first; the plain name is the fallback for
      series whose article carries no suffix. With the fix, the 9-1-1 series
      article contributes 30 spoiler-free facts.
    """
    fetch = fetch or wikipedia.fetch_extract
    if metadata.get("type") == "episode":
        series = wikipedia_title(metadata)
        extract = fetch(f"{series} (TV series)", None)
        if extract:
            return extract, f"{series} (TV series)", None
        return fetch(series, None), series, None
    title = wikipedia_title(metadata)
    year = metadata.get("year")
    return fetch(title, year), title, year


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rating-key", required=True)
    parser.add_argument("--base-url", default=os.environ.get("PLEX_BASE_URL", ""))
    add_model_arguments(parser)
    parser.add_argument("--extract-command", default="",
                        help='placeholders are substituted shell-quoted, so do '
                             'not quote them yourself, e.g. "ssh plex ffmpeg '
                             '-v error -i {media_path} -map 0:s:0 -f srt {output}"')
    parser.add_argument("--srt-file", default="",
                        help="use this saved SRT instead of acquiring a transcript "
                             "(sidecar/extractor); for rebuilds when a downloaded "
                             "sidecar has vanished from Plex")
    parser.add_argument("--wikipedia", default="auto", choices=["auto", "off"])
    parser.add_argument("--entity-expansion", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="entity lookups (per-actor facts + per-segment "
                             "context notes); defaults to the entity_expansion setting")
    parser.add_argument("--jev", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="optional Jev entity selection (a decision model "
                             "chooses which entities are worth expanding before "
                             "the fetch); defaults to the jev_enabled setting. "
                             "With no key configured this is a no-op, exactly "
                             "as if it were off")
    parser.add_argument("--out", default="")
    parser.add_argument("--store-db", default="",
                        help="persist the build into this SQLite file (plugin data.db)")
    parser.add_argument("--debug-dir", default="",
                        help="save every prompt and raw response here")
    parser.add_argument("--max-segments", type=int, default=0,
                        help="stop after N segments (fast quality check)")
    parser.add_argument("--show", type=int, default=5, help="how many messages to print")
    args = parser.parse_args()

    token = os.environ.get("PLEX_TOKEN")
    base_url = args.base_url or os.environ.get("PLEX_BASE_URL", "")
    if not token or not base_url:
        # Same credentials the watcher saved, so the CLI works without exports.
        from watch_with_me import store
        creds_file = store.data_dir() / "plex-credentials.json"
        if creds_file.exists():
            creds = json.loads(creds_file.read_text("utf-8"))
            token = token or creds.get("token")
            base_url = base_url or creds.get("base_url", "")
    if not token:
        raise SystemExit("PLEX_TOKEN is not set and no saved credentials were found")
    if not base_url:
        raise SystemExit("--base-url or PLEX_BASE_URL is required")

    client = PlexClient(base_url, token, "watch-with-me-build", allow_loopback=True)
    metadata = client.metadata(args.rating_key)
    if not metadata:
        raise SystemExit(f"no metadata for ratingKey {args.rating_key}")

    settings = load_settings()
    if args.extract_command:
        settings.extract_command = args.extract_command
    if args.entity_expansion is not None:
        settings.entity_expansion = args.entity_expansion
    if args.jev is not None:
        settings.jev_enabled = args.jev
    if args.srt_file:
        srt = Path(args.srt_file).read_text("utf-8", errors="replace")
        source = "file"
    else:
        with tempfile.TemporaryDirectory(prefix="wwm-build-") as tmp:
            srt, source = build.acquire_transcript(metadata, client, settings, Path(tmp))
    print(json.dumps({"title": metadata.get("title"), "year": metadata.get("year"),
                      "transcript_src": source, "chars": len(srt)}), flush=True)
    if not srt:
        print("no transcript: sidecar missing and no extractor configured", file=sys.stderr)
        return 2

    sentences: list[str] = []
    article_cache = wikipedia.ArticleCache()

    def cached_extract(title: str, year: int | None = None) -> str:
        query = f"{title} ({year} film)" if year else title
        try:
            return wikipedia.fetch_article(query, cache=article_cache) \
                .get("extract") or ""
        except Exception:  # noqa: BLE001 - a lookup failure is an empty source
            return ""

    if args.wikipedia == "auto":
        try:
            extract, query_title, query_year = fetch_article_extract(
                metadata, fetch=cached_extract)
            sentences = wikipedia.section_facts(extract)
            metadata["_wikipedia_url"] = wikipedia.article_url(query_title, query_year)
        except Exception as exc:  # noqa: BLE001 - optional source
            print(f"wikipedia unavailable: {exc}", file=sys.stderr)
    print(json.dumps({"wikipedia_sentences": len(sentences)}), flush=True)

    person_facts: list[dict] = []
    if args.wikipedia == "auto":
        try:
            person_facts = fetch_person_facts(
                metadata, settings.entity_expansion,
                lambda name: wikipedia.fetch_article(name, cache=article_cache))
        except Exception as exc:  # noqa: BLE001 - optional source
            print(f"cast facts unavailable: {exc}", file=sys.stderr)
    print(json.dumps({"person_facts": len(person_facts)}), flush=True)

    model, choice = pick_model(settings, args.model)
    print(json.dumps({"model": model, "model_source": choice.source}), flush=True)
    complete = make_completer(model, effective_reasoning(args.reasoning, settings))
    if args.debug_dir:
        complete = with_debug(complete, Path(args.debug_dir))

    def progress(index: int, total: int, totals: dict) -> None:
        print(f"  segment {index}/{total}  returned={totals['returned']} "
              f"anchor_miss={totals['anchor_miss']} rejected={totals['rejected']}",
              flush=True)

    result = build.build_messages(metadata, srt, sentences, complete,
                                  max_segments=args.max_segments or None,
                                  person_facts=person_facts,
                                  on_segment=progress,
                                  expander=build_expander(settings, complete, metadata,
                                                          wikipedia=(args.wikipedia == "auto")))
    result.transcript_src = source

    summary = {
        "state": result.state,
        "segmentation": result.segmentation,
        "segments": result.segments,
        "messages": len(result.messages),
        "withheld": len(result.withheld),
        "stats": result.stats,
        "fetch_stats": wikipedia.fetch_stats(),
        "error": result.error,
    }
    print(json.dumps(summary, indent=2))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({
            "summary": summary,
            "messages": result.messages,
            "withheld": result.withheld,
        }, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")

    if args.store_db:
        from watch_with_me import store

        conn = store.connect(Path(args.store_db))
        build.persist(conn, result, metadata)
        print(json.dumps({"stored": str(args.store_db),
                          "messages": len(result.messages)}))
        conn.close()

    for message in result.messages[:args.show]:
        minutes, seconds = divmod(message["t_ms"] // 1000, 60)
        print(f"  {minutes:>3}:{seconds:02d}  [{message['kind']}] {message['text']}")
    return 0 if result.state == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
