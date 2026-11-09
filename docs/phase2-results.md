# Phase 2 results: transcript, segmentation and cue generation

Completed 2026-11-06 against The Cycle (ratingKey 65481), a real Plex title with a
downloaded sidecar subtitle.

## Measured quality

Twelve segments, about 36 minutes of film:

```
returned          22
resolved          20
anchor_miss        2
rejected           0
anchor_hit_rate  0.909
kept               3   (after the six-minute density window)
retries            1
```

Sample output, all anchored to real subtitle lines:

```
 2:13  [cast] Don Keith Opper plays Frank.
15:57  [cast] Jeb Berrier plays coroner Joe Ratterman.
28:46  [production] The Cycle was produced by Tea Shop Productions.
```

The 0.85 gate is met. The two anchor misses are ordinary fuzzy-match failures, not
invented timestamps: nothing was placed without a matching line.

## What was built

| Module | Role |
|---|---|
| `transcript.py` | SRT parsing with comma decimals, dialogue-gap and chapter segmentation, extractor runner with `shell=False` |
| `cues.py` | Fact sheet, prompt contract, schema validation with per-reason counters, anchor resolution, dedupe, density, model-only cap |
| `build.py` | Transcript acquisition (sidecar then extractor), generation, persistence in one transaction |
| `sources/wikipedia.py` | Production-sentence extraction with a descriptive User-Agent |
| `scripts/build_title.py` | End-to-end CLI with progress, `--max-segments`, `--debug-dir`, `--reasoning` |

68 tests pass, including the transcript, prompt-format, validation, density and
persistence paths.

## The failure that mattered

The first runs rejected nearly everything. Two separate causes, both found by evidence
rather than guesswork:

1. **Empty model responses.** `finish_reason=length` with `reasoning_tokens=2500` equal to
   the cap: the reasoning model deliberated until the budget ran out and wrote nothing.
   Fixed with reasoning effort `low`, a fallback to reasoning disabled, and a retry that
   treats an empty body as transport-shaped rather than as "no messages".
2. **A prompt-format collision.** Fact lines and transcript lines both began with `[...]`,
   so anything scanning brackets could confuse them. Facts now use `f3 (wikipedia): ...`
   and only transcript lines keep `[seconds]`.

A third defect was mine: `anchor_hit_rate` divided kept messages by returned messages,
which made a good build look broken because density prunes on purpose. It now reports
`resolved / returned` and lists `resolved` separately.

## Still open

- The full 40-segment build of a feature has not been run end to end, only 12 segments.
- The anime episode with 32 embedded subtitles still needs the extractor path proven on
  real media.
- Wikipedia gave 8 usable sentences for this title. Thin source coverage is expected and
  the fact sheet leans on Plex credits for cast.
