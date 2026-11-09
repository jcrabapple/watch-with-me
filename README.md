# Watch With Me

A Hermes plugin that watches Plex with you.

While something plays, it follows the session, reads the subtitle track, and
publishes short sourced notes into a page on your phone: who that actor is,
what else they were in, where the scene was shot, the song that just started.
Notes are anchored to the line being spoken, so they arrive with the moment
rather than on a timer.

It is a second screen that knows what you are watching and will not spoil it.

## What it does

- **Follows the session.** Polls Plex, claims the playback you care about,
  predicts the playhead between Plex's ten-second heartbeats, and survives
  pauses, seeks, restarts, and the same player starting a different title.
- **Anchors every note to a real line.** The model never emits a timestamp; it
  copies a line from the transcript, and code resolves that line to a time. An
  anchor that cannot be found is dropped, not guessed.
- **Keeps spoilers out.** The transcript is cut at the segment end before the
  model sees it. Every claim carries a release state, character names stay
  quarantined until the dialogue establishes them, and anything that could
  reveal a later plot point is withheld until the credits.
- **Answers when asked.** One button: *what did I just see?* It takes the
  moment, the elapsed dialogue, and the claims that are safe right now, and
  either answers with a sourced note or says it has nothing solid.
- **Prepares ahead.** It reads Continue Watching and builds notes for the
  queue in the background, one title at a time, so nothing starts unprepared.

## Install

```bash
hermes plugins install jcrabapple/watch-with-me
hermes gateway restart
```

Then, in Hermes, run `/watch setup` (or open the plugin's dashboard pane) and
link your Plex account with the four-digit code it shows at
<https://plex.tv/link>. The plugin discovers your servers, probes each
advertised connection, and verifies your library before saving anything.

## Where the transcript comes from

Notes need text. In order of preference:

1. **A subtitle file Plex already has.** Free and instant.
2. **An extract command** you configure, for subtitles embedded in the video
   file. Example for a media host you can reach over SSH:

   ```bash
   ssh host "ffmpeg -v error -i {media_path} -map 0:s:0 -f srt -"
   ```

   `{media_path}`, `{stream_id}` and `{output}` are substituted already quoted;
   leave the placeholders bare.
3. **Plex's own subtitle search**, opt-in and off by default. It writes a
   subtitle into your library and needs Plex Pass, so it only ever runs when
   you turn it on. The plugin keeps its own copy of the text, because Plex
   treats those files as temporary.

If a title has no usable text track and neither of the last two is configured,
it is reported as unsupported with the reason. That is a successful detection,
not a failure.

## Choosing a model

Notes are written by the model behind the `watch_with_me_cues` auxiliary task.
Pick it in the auxiliary-model picker (the `auxiliary.watch_with_me_cues`
entry); on a fresh install it inherits the `compression` task, which is
usually a cheap model already.

Cost shape: one call per transcript segment, roughly forty per film, doubling
when entity expansion is on. A flash-tier model is sufficient. A reasoning
model costs several times more and can return empty content when its token
budget goes to reasoning.

Resolution order, first match wins: an explicit `note_model` setting (only
when the host allows model overrides) → the picker entry → the `compression`
inheritance → for standalone CLI builds only, `WWM_MODEL` then
`OPENROUTER_MODEL`. The dashboard settings area shows the resolved model and
where it came from.

## Optional: Jev for entity selection

With a TypeSafe or OpenRouter key, the plugin can ask [Jev](https://typesafe.ai)
which real-world entities mentioned in a scene are worth expanding — a
librarian's judgment about what a viewer would actually look up. It is strictly
optional: with no key, no network and no `hermes-jev` installed, installation,
tests and builds behave exactly as they do without it. The key is write-only
and stored at mode 0600; it is never returned by the API or written to a log.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `note_display` | `live` | `live`, `pause`, or `both`. See below. |
| `note_model` / `note_provider` | empty | Explicit model override, if the host allows it. |
| `prepare_depth` | `off` | `off`, `next`, `two`, or `queue` — how far ahead to prepare. |
| `prepare_budget_titles_per_day` | `3` | Daily cap on background builds. |
| `download_subtitles` | `false` | Let Plex fetch subtitles into your library. Needs Plex Pass. |
| `allowed_user_ids` / `allowed_player_ids` | empty | Keep notes to your own streams on a shared server. |
| `jev_enabled` / `jev_api_key` | `false` / empty | Optional entity selection. The key is write-only. |

### Note display modes

- `live` — notes appear as they arrive. This is the default so a demo shows
  the feed working.
- `pause` — the calm mode, and the one a normal viewer should pick. Notes are
  still delivered and recorded, so nothing is lost or re-sent, but the page
  holds them while the film plays and shows a quiet "3 notes waiting".
  Pausing reveals them with timestamps and sources; tapping the counter while
  playing reveals them too.
- `both` — live rendering with the counter hidden.

The end-of-session digest is unchanged in every mode.

## What it does not do

- It cannot read subtitles that are only embedded in the video over HTTP. Plex
  does not serve them; use an extract command.
- It cannot read picture-based subtitles (PGS, VobSub). That would need OCR.
- It is not frame-accurate. Notes land within a few seconds of their line.
- It cannot promise that a model with prior knowledge of a film will never
  reveal something. The escrow machinery bounds what reaches it, and the
  credits artifact holds the rest.
- It does not see the picture. A note can quote the dialogue and the sources;
  it cannot tell you what is on screen.

## Development

```bash
python -m pytest -q                       # 410 tests
python scripts/build_title.py --rating-key <key> --out notes.json
python scripts/run_watcher.py             # the poll loop, standalone
```

The build pipeline also runs against a real Plex server for a single title,
which is how note quality is measured.

## License

MIT.
