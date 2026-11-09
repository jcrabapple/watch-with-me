# Phase 0 results (partial, 2026-11-05)

Scope: prove the plugin shape end to end without Plex credentials. The live
measurements (cadence, discovery payload, sidecar classification) are still open
and are listed at the bottom with the exact command.

## What was built

```
plugin.yaml, __init__.py
watch_with_me/{config,store,plex,policy,scheduler,watcher,plugin}.py
dashboard/{manifest.json,plugin_api.py,app.html,app.js,app.css}
scripts/{probe_plex.py,serve_router.py}
tests/{test_policy,test_scheduler,test_store}.py
```

Nineteen tests cover the pure scheduler and policy logic plus migrations,
heartbeat, claim persistence, and message ordering.

## Proven, with evidence

| Claim | Evidence |
|---|---|
| Policy and clock logic are testable without a server | `19 passed in 0.86s` |
| Migrations are idempotent and recorded once | `test_migrations_are_idempotent` |
| The watcher starts, heartbeats, and refuses a second instance | first `start(): True`, second `start(): False`, state `unconfigured`, heartbeat fresh |
| Sustained poll failure enters `disconnected` | three refused connections -> `watcher_state=disconnected` |
| The watcher stops cleanly through the unload hook | `stopped cleanly: True` |
| Registration matches the manifest exactly | 4 tools, `/watch`, one aux task, one unload hook; manifest declares the same 4 tools |
| The router serves the page with a strict CSP | `default-src 'none'; script-src 'self'` … `frame-ancestors 'none'`, `nosniff`, `no-referrer` |
| Static assets load with correct types | `app.js` 200 `text/javascript`, `app.css` 200 `text/css` |
| `/session` and `/messages` return live rows | seeded session renders as `live`/`claimed`, message returned in order |
| `/control` mutates state | `{"ok":true}` then `muted=True` |

## Corrections to the plan

1. **`dashboard/manifest.json` is required.** The plan said the backend-only
   router needs no manifest. Hermes discovers the API file from
   `dashboard/manifest.json`'s `api` key (`web_server_dashboard.py`, `_dashboard_plugin_entry`),
   so the file is mandatory. It uses `"tab": {"hidden": true}` and therefore adds
   no tab.
2. **Claim persistence needs an explicit upsert column.** The session upsert must
   write `claimed`/`claimed_at`, and the watcher must adopt a claim stored in
   `runtime_state` after a restart. Without both, `/watch start` is undone by the
   next poll.
3. **Validation is fine; my first run was poisoned by local artifacts.** The first
   `hermes plugins validate` took 15 minutes and failed its security scan because I had
   created `.venv/` and `.phase0/` inside the plugin directory: the scanner walks the
   whole tree, so it reported thousands of findings from site-packages (crypto `.so`
   files, Google API JSON, click's `sudo` strings). After moving the virtualenv to
   `~/.venvs/watch-with-me` and deleting `.phase0/`, the same command completes in
   seconds and passes every check: manifest, requires_hermes, loadable, python
   dependencies, capability probe, declared tools/hooks/middleware, built-in
   collisions, no core override, and `Isolation — runs in the plugin host`.
   One caution remains: `env_exfil_curl` on two probe commands in `RESEARCH.md`, which
   is a research artifact, not shipped code. A copy of the tree without the design
   documents (`PLAN*.md`, `RESEARCH.md`, `REVIEW.md`, `SUMMARY.md`) validates with
   **`security scan — safe`, zero warnings**. Packaging must exclude those files.
   Keep development virtualenvs outside the plugin directory.
4. **`scripts/probe_auth.py` and `scripts/probe_cues.py` do not exist yet.** Phase 0
   lists them; only `probe_plex.py` and the router harness were built. The plan now
   marks their command blocks as intended interfaces rather than runnable steps.

## Measured (U1, Plex Web)

Collected 2026-11-05 during real playback on the author's server, 176 samples over
180.7 s, plus a 59-sample paused run. The first summary the script printed was wrong:
it measured the time between the samples bracketing an update (always the 1 s polling
interval) instead of the time between update events. Recomputing from the same JSONL:

| | Playing | Paused |
|---|---|---|
| Update interval, median | 10.17 s | none |
| Update interval, p95 / max | 10.3 s / 10.3 s | none |
| Offset jump per update | 10,000 ms | 0 |
| Updates in window | 18 | 0 |
| Stalls over 30 s | 0 | n/a |
| Offset advanced vs wall clock | 179.0 s in 179.7 s | 0 |

Conclusions now fixed in the plan:

- `viewOffset` is a 10-second heartbeat on Plex Web, not a clock. Prediction between
  updates is what provides continuity.
- Every playing snapshot rebases; the threshold only classifies a jump as a seek.
  `T = 2 x p95 = 20,600 ms` for this client.
- Pause handling is confirmed: zero updates and a frozen offset, which is the state the
  watcher freezes on.
- The "within a few seconds" timing promise holds for Plex Web. Cue error is bounded by
  clock drift between updates, not by the 10 s cadence.

Other clients (Android TV, Roku, HTPC) are still unmeasured. Their cadence is stored per
`Player.machineIdentifier` rather than assumed to match Plex Web.

## Metadata probe (U4, The Cycle, ratingKey 65481)

```
Guid        imdb://tt32358390  tmdb://1290418  tvdb://376402
chapterSource   null
Chapter         []
audio           eac3, eng, selected
subtitles       id 239008, codec srt, lang eng, key null, location null
```

What this settles:

- **`includeGuids=1` works.** All three external ids arrive. The id chain in the plan is
  sound.
- **Chapters are not guaranteed.** This title has none, and `chapterSource` is null. The
  plan's original "chapters only" segmentation would produce no timed cues for it, so
  segmentation is now a chain: chapters, then dialogue-gap blocks, then labelled fixed
  windows. This is a reversal of the earlier de-scope, made on evidence.
- **The subtitle was embedded, and Plex fixed it.** Initially `codec: srt` with `key: null`
  and `location: null`, an embedded track the sidecar route cannot fetch (HTTP 501). After
  downloading subtitles from Plex Web, the item reports two text streams, one with a key,
  and `transcript: sidecar`. The public recommendation is therefore: turn on Plex subtitle
  download, and most embedded-only titles become usable without any plugin-side extractor.
- `location: wan` on the playing session means Plex is not a reliable LAN/WAN signal for
  player filtering.

Still open: whether requesting `/library/streams/239008.srt` returns the track anyway
(some PMS builds serve embedded SRT through that route despite the contract), and whether
chapters are missing across the library or only on this file.



## Episode probe (Google TV, ratingKey 64780)

```
title             Nawaam Attacks
series            Star Wars: Visions Presents - The Ninth Jedi
season/episode    1 / 4
player            Basement TV 2
machineIdentifier e3de12d48c586206-com-plexapp-android
location          wan
chapters          1
text subtitles    32, all embedded, 0 sidecars
```

What this settles:

- **Google TV reports on the same 10 s heartbeat.** Median 10.19 s, p95 10.31 s, exact
  10 s jumps, no stalls. The rebase threshold stays global at 20,600 ms; per-client
  storage is unnecessary.
- **Episode fields parse.** `grandparentTitle`, `parentIndex`, `index` all arrive through
  the same session object as movies.
- **One chapter is not chapters.** This episode has a single chapter spanning the file,
  which the "at least four" rule correctly rejects in favour of dialogue-gap
  segmentation. Chapter *count* needs checking, not just presence.
- **Embedded-only is a real library shape.** 32 text subtitle tracks, every one embedded,
  no sidecar. This is the hard case: the sidecar route answers 501 for each, so the title
  is metadata-only unless a sidecar appears or an extractor is configured.

Open question that decides the public story: whether Plex's subtitle download creates a
sidecar for an item that **already** has embedded subtitles, or considers it satisfied and
does nothing. The movie case had no subtitles at all, so it does not answer this.



## Transcode subtitle route (tested, dead end)

With playback running on Google TV, subtitles selected, and a live transcode session
(`/transcode/sessions/1771562c-…`), the embedded track still could not be fetched over
HTTP:

| Variant | Result |
|---|---|
| `session=<uuid>` | connection accepted, **0 bytes in 30 s** |
| `session=<full key>` | HTTP 400 |
| `transcodeSessionId=<uuid or key>` | HTTP 400 |
| `subtitles=embedded`, `subtitlesStreamID=<id>` added | still 0 bytes |

The route accepts only the bare UUID and then serves nothing, so it is pacing off the
transcode buffer rather than exposing the track. **Do not build on it.** Embedded
subtitles are unreachable over HTTP on this server, full stop.

Consequence for the product: a title with no sidecar has no transcript unless the user
supplies one. That makes the setup flow responsible for coverage, not the plugin.



## Discovery probe (U2, settled)

One server returned, with `provides` equal to the string `server`, exactly as the
research predicted. The connections are the interesting part:

```
172-17-0-1 … 172-26-0-1.plex.direct:32400   local: true   (docker bridges, unreachable)
192-168-80-1, 192-168-64-1.plex.direct:32400 local: true  (docker bridges, unreachable)
51-81-84-54.plex.direct:32400                local: false  (the real public endpoint)
45-33-90-120.plex.direct:8443                relay: true   (Plex relay fallback)
```

**`local: true` is not a reachability signal.** Nine of the eleven URIs are container
addresses that answer only from inside the Plex host. A plugin that prefers the first
`local` connection would pick `172.17.0.1` and fail. The onboarding flow must probe each
URI with `GET /identity` on a short timeout and take the first that answers, using
`local` only to order the attempts. This is now written into the plan.

Second observation: the working URI is HTTPS with a `plex.direct` hostname, so the client
must accept a real certificate rather than assuming plain HTTP.



## Still unmeasured (U5, U6)

U5 (phone page with the dashboard cookie) and U6 (WebSocket survival on a backgrounded
phone) need a LAN bind and a real device. Everything else in Phase 0 is settled:

| Item | Result |
|---|---|
| U1 `viewOffset` cadence | 10.17 s median, p95 10.3 s, no stalls; paused frozen |
| U2 discovery shape | `provides` is a string; `local` is not a reachability signal |
| U3 sidecar subtitles | embedded returns 501; a Plex-downloaded sidecar fetched, 1,199 cues |
| U4 external ids | `imdb://`, `tmdb://`, `tvdb://` all present with `includeGuids=1` |
| Chapters | absent on the test title, so segmentation needs the gap fallback |

## What this means for Phase 1

The scheduling model can be coded now: its constants are configurable and the tests pin
the "reporting lag is not a seek" behaviour. The claim rule, the disconnected state and
the polling endpoints are already implemented. Phase 1's remaining work is the real
session state machine, the seek suppression rule, and migrations against a populated
fixture.
