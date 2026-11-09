# Phase 6 — Thin titles: which levers recover facts without risking spoilers

Measured 2026-11-06 against the phase-4 controls: **The Cycle** (ratingKey
65481, 5,941-char article — thin) and **Sinners** (64341, 25,671 chars —
rich), plus one real episode (**9-1-1 S10E1 "That Sinking Feeling"**,
65130) for the series lever. All raw evidence is in `artifacts/phase6/`
(`measure.json`, `measure.py`, per-lever `lever*-*.txt` dumps); spoiler
admission was checked by scanning the admitted sentences for
`SPOILER_TERMS`, not assumed.

## Results

| # | Lever | Facts gained — The Cycle | Facts gained/lost — Sinners | Spoiler-bearing sentence admitted? | Decision |
|---|-------|--------------------------|-----------------------------|------------------------------------|----------|
| 1 | Sentence floor 40 → 30 / 25 | **+0 / +0** | +1 / +2 | No — but the new sentences are junk (below) | **Rejected** |
| 2 | Series fallback for episodes | n/a (episode control) | n/a (episode control) | No (30 facts scanned clean) | **Implemented: 0 → 30 facts on the episode control** |
| 3 | Lead section | already included; 3 of 4 lead sentences survive | already included; 8 of 15 survive | Removing the hint gate admits premise/plot text | **Rejected** (kept as-is) |
| 4 | Adjacent article (director / studio) | director: **0** (no article); studio: 4 but identity ambiguous | director: 8; studio: 4 | No spoiler terms, but see below | **Rejected** |
| 5 | Plex `tagline`, `Genre`, `Country`, `audienceRating`, `Collection` | **+4** | +4 (0 displacement on capped sheets) | Impossible by construction — local metadata | **Implemented** |

## Why The Cycle is thin (the structural diagnosis)

The 5,941-char article splits into: lead 4 sentences, Production 2, Cast 2,
**Plot 33**, Reception 5. The allow-list drops Plot/Cast/Reception wholesale
— correctly; that is the global trade phase 4 bought on purpose. What
remains is 6 sentences, 5 of which pass the quality gates. **No lever that
keeps the spoiler-safe section filter can change this:** the missing
material is plot and reception, and it must stay missing. The thin-title
floor for Wikipedia-sourced facts on The Cycle is ~5. The recovered ground
is Plex metadata (+4 lines) and, for episodes, the series article.

## Lever 1 — sentence length floor: rejected

Facts are 5 / 5 / 5 on The Cycle at floors 40 / 30 / 25 — the floor is not
what rejects Cycle sentences. On Sinners the floor at 25 admits +2
sentences, both junk (`lever1-sinners-new-at-25.txt`):

- `Jordan cast in the lead role.` (29) — a split artifact of the name
  "Michael B. Jordan"; the sentence splitter cut mid-name.
- `Sony Pictures, Warner Bros.` (27) — a list tail, no verb.

At 30: `Additional roles were cast in April.` (+1, filler). The 40-char
floor is doing real work; the thin control gains nothing. **Rejected: 0 on
the control that matters.**

## Lever 2 — series/article fallback for episodes: implemented

`wikipedia_title()` was already fixed to use `grandparentTitle`, but the
episode path had two further defects, both measured on the 9-1-1 control
(`measure.json → lever2_series_fallback`):

1. **The episode year was passed as a film year.** `fetch_extract(title,
   year)` appends "(year film)", so an episode queried `"9-1-1 (2026
   film)"` → **0 chars**. Every episode build to date got no Wikipedia
   facts at all.
2. **A bare series name can be a different primary topic.** `"9-1-1"`
   resolves to the *emergency telephone number* — 13,877 chars whose
   sections are History/Enhanced 911/Funding/Problems, yielding **0 safe
   facts** and a live risk of notes about the wrong subject. (The episode's
   own title is worse: "That Sinking Feeling" is a 1979 Scottish comedy
   film — 3,949 chars about something else entirely.)

Fix in `scripts/build_title.py:fetch_article_extract()`: for episodes, try
`"{series} (TV series)"` first (no year), fall back to the plain series
name; movies unchanged. Same downstream filter untouched. Measured after
the fix on the real episode: **30 spoiler-free facts** (lead + production),
verified live. Movies verified byte-identical behaviour (Cycle 5, Sinners
66 facts).

## Lever 3 — lead section: rejected, kept as-is

The lead is **already included**, gated to production-hint sentences.
Survival measured: The Cycle 3/4 lead sentences, Sinners 8/15 — the lead is
not the thin-title bottleneck either.

Removing the hint gate was measured (`lever3-*-lead-without-hints.txt`):
Cycle +1 ("The movie blends crime procedural elements with
creature-feature horror." — premise-flavoured), Sinners +5, of which two
are a sentence-split artifact ("…it stars Michael B." / "Jordan in dual
roles …") and one carries the premise reveal ("confronted by a vampiric
threat") — the spoiler vocabulary list does not catch premise text. The
other three are genuinely production-flavoured ("The project was announced
…", "Pictures acquired distribution rights …", "Principal photography …"),
which suggests PRODUCTION_HINTS could grow ("photography", "announced",
"distribution") — but that refinement gains **0** on The Cycle, so it is
out of scope for this card and recorded here for a rich-title card.
**Rejected: loosening admits premise material; the gate stays.**

## Lever 4 — adjacent article rescue: rejected

Measured both readings on both controls:

- **Director, thin control: 0.** Jordan Downey has no Wikipedia article
  (0 chars). The "from the director of" lever cannot help The Cycle at all.
  That is the acceptance number and it kills the lever on its own.
- **Director, rich control: 8 facts** from Ryan Coogler's article under the
  same filters — but one admitted sentence is a garbled table caption
  ("Directed Academy Award performances Under Coogler's direction, these
  actors have received…"), showing person-article structure leaks through
  the film-shaped filters. The person-shaped tool (`cast.lead_sentence`)
  already exists and is what person facts use.
- **Studio: 4 facts each**, but studio-name identity is unverifiable from
  Plex metadata alone: "Tea Shop Productions" resolves to a London company
  ("formerly The Tea Shop & Film Company") that may or may not be the
  US/UK production credited on The Cycle — a misattribution vector straight
  into the fact sheet.

**Rejected: 0 facts on the thin control via the director; studio identity
ambiguous.** One extra request per build for a rich-title-only gain with a
table-caption leak is not the thin-title fix.

## Lever 5 — free Plex facts: implemented

`tagline`, `Genre`, `Country`, `audienceRating`, `Collection` are already
in the Plex metadata response — no network call, no third-party source,
spoiler-safe by construction. Measured lines on both controls
(`lever5-*-lines.txt`): The Cycle +4, Sinners +4. `Collection` was absent
on both. Implemented in `cues.build_fact_sheet()` as `plex`-sourced lines
placed **after all credit lines**, so a fact sheet that hits `max_facts`
loses these first and never displaces Wikipedia material — verified on the
real Sinners metadata (sheet capped at 60, all Wikipedia-sourced) and on
The Cycle (sheet 25, 4 new lines present).

## Honest bottom line

The Cycle's Wikipedia floor is structural: 80% of its article is Plot and
Reception, and the spoiler-safe filter is correct to drop them. What this
phase recovered, measured:

- **The Cycle: +4 facts** (tagline, genres, countries, audience rating) —
  no network, no spoiler risk. Wikipedia facts stay at 5 by design.
- **Every episode build: Wikipedia facts 0 → 30** on the measured control,
  by fixing the "(year film)" suffix bug and the wrong-primary-topic
  resolution. Episodes were the real thin-title casualty and they are
  fixed.
- **Sinners: +4 sheet lines, 0 displacement** of Wikipedia facts.

Nothing here loosens the global section filter; levers 1, 3 and 4 are
documented as rejected with the numbers that killed them.

## Verification

- Full suite: **217 passed** (was 211; +6 new tests:
  `TestFetchArticleExtract` ×3, fact-sheet Plex-facts ×3).
- Real-data re-run after implementation: episode → 30 facts via
  `"9-1-1 (TV series)"`; Cycle/Sinners movie queries unchanged; Cycle
  sheet +4 lines; Sinners capped sheet unaffected.
- No writes to the plugin `data.db`; watcher, page server and gateway
  untouched; no new third-party sources.
