# After install

Watch With Me is installed. It watches Plex sessions and builds sourced
film-craft notes for what you watch.

## Choosing a model

Notes are written by the model selected for the `watch_with_me_cues`
auxiliary task. In the desktop or mobile app, open the auxiliary-model picker
(the `auxiliary.watch_with_me_cues` entry in the Hermes config) and pick a
model there.

Cost shape: the notes task runs once per transcript segment — about 40 calls
per film — and doubles if entity expansion is on. **A flash-tier model is
sufficient**; a reasoning model costs several times more and can return empty
content when the token budget goes to reasoning tokens.

If you want a specific model regardless of the picker, set the plugin setting
`note_model` (and optionally `note_provider`) — but only after allowing model
overrides on the host, or the override is silently ignored:

```yaml
plugins:
  entries:
    watch-with-me:
      llm:
        allow_model_override: true
      settings:
        note_model: "your/model"
```

When the picker entry is absent, the task inherits the `compression`
auxiliary task's model. Standalone builds (`scripts/build_title.py`) fall back
to the `WWM_MODEL` environment variable, then `OPENROUTER_MODEL`; inside
Hermes the environment is never consulted.

The dashboard page's settings area always shows the resolved model (or "not
configured"), its source, and one sentence of guidance.

## The dashboard page

Open the **Watch With Me** tab in the Hermes Dashboard sidebar: it renders
the plugin page, shows what your Plex server is playing, and carries the
settings drawer, the Plex link flow, and the note feed. On a phone, use the
LAN link the page offers (or set `public_base_url` to an `http(s)://` URL
you reach from outside the network). Everything below can be done from that
page; the terminal is only needed for the chat-handoff snippet.

## Chat handoff (optional)

If you want playback start to drop the companion link into the chat session
that started the watcher, grant the host capability and enable the setting:

```yaml
plugins:
  entries:
    watch-with-me:
      allow_gateway_injection: true   # host capability, config.yaml only
      settings:
        chat_notifications: true
```

`allow_gateway_injection` cannot be set from the UI; it must be granted in
`config.yaml`. The page's settings area shows whether both halves are in
effect (Settings → Plugins → Watch With Me will tell you the same).

## Choosing when notes appear

The `note_display` setting controls when the page shows notes: `live`
(default, best for demos), `pause` (notes accumulate silently while the film
plays — the page shows a quiet counter — and are revealed, newest first with
timestamps and sources, when you pause or tap the counter), or `both` (live
rendering, counter hidden). Delivery and the end-of-session digest are
unchanged in every mode; a normal viewer will likely prefer `pause`:

```yaml
plugins:
  entries:
    watch-with-me:
      settings:
        note_display: "pause"
```

## Linking your Plex account

Watch With Me never asks for a Plex token. Open the plugin's dashboard page
and press **Link Plex account**: a four-digit code appears; enter it at
[plex.tv/link](https://plex.tv/link) on any device. The plugin polls Plex,
finds the server that actually answers (shared servers are labelled "shared
with you"), counts your libraries, samples ten recent titles for subtitle
coverage, and only then stores the token — mode `0600`, never in the config
file, never in a log. `/watch setup` runs the same flow in text form.

If Plex later rejects the token (for example after you revoke access), the
page says so and offers **Relink**; the watcher picks up the new token on its
next poll — no restart needed.

Developers can still store a token manually:
`PLEX_TOKEN=... scripts/probe_plex.py save-credentials --base-url http://<server>:32400`
