# Phase 1 results: core playback loop

Completed 2026-11-06. 45 tests pass. Packaged tree validates with `security scan —
safe` under `plugins.isolation: host`.

## What was built

| Module | Role |
|---|---|
| `machine.py` | The session state machine with no I/O: candidate, claim, live, paused, observed, ended, seek suppression, manual-stop suppression |
| `watcher.py` | Poll loop, credentials, file lock, heartbeat, failure backoff, persistence |
| `scheduler.py` | Coarse-anchor clock, due selection, seek classification |
| `store.py` | Schema v2, delivery ledger (`shown`/`skipped`), timeline, view counts, dismissals |
| `dashboard/plugin_api.py` | `/session`, `/messages` from the delivery ledger, `/control`, `/feedback` |
| `plugin.py` | Tools and `/watch`, watching start/stop wired to the same state |

## Acceptance criteria

| Criterion | Evidence |
|---|---|
| Test suite passes | 45 passed |
| A playing item goes live and a due message is delivered once | `test_watcher_goes_live_and_delivers_once` drives a real HTTP server and asserts one delivery |
| Pause freezes and emits nothing | `test_pause_freezes_and_emits_nothing` |
| Small drift is not a seek; a real jump is | `classify_sample` tests with the measured 20,600 ms threshold |
| Forward seek suppresses, backward does not duplicate | `test_forward_seek_suppresses_the_messages_it_crossed`, `test_backward_seek_does_not_repeat_delivered_messages` |
| Two streams: the first claims, the second is observed | `test_second_stream_is_observed_and_cannot_steal_the_claim` |
| Three failed polls enter `disconnected` | `test_three_failed_polls_enter_disconnected` against a dead port |
| A populated v1 database upgrades | `test_v1_database_upgrades_and_keeps_delivery_rows` |
| Manual stop does not get re-claimed | `test_a_manual_stop_suppresses_re_claiming_the_same_stream` |
| Page carries a strict CSP | `test_page_carries_a_strict_csp`, no `unsafe-inline` |

## Deliberate gaps

- **WebSocket `/events` is not implemented.** The page polls, which the plan makes
  first-class. The socket stays an accelerator to add once the page exists.
- **Host-isolation runtime check** is a Phase 5 item; the validator reports the plugin as
  host-capable, and the API is designed for buffered responses.
- **Recovery from `disconnected`** is implicit: the next successful poll overwrites the
  session state. No test drives a disconnect-then-recover cycle yet.

## What Phase 2 must add

The build pipeline: sources, transcript chain with the extractor, segmentation,
cue generation with anchor resolution, digest. Nothing in Phase 1 depends on it, and the
delivery ledger already supports messages that exist ahead of time.
