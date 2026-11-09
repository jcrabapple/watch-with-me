/* Watch With Me: one-sided stream. No inline script, no HTML string injection. */
'use strict';

const els = {
  title: document.getElementById('title'),
  subtitle: document.getElementById('subtitle'),
  stream: document.getElementById('stream'),
  status: document.getElementById('status'),
  wake: document.getElementById('wake'),
  mute: document.getElementById('mute'),
  end: document.getElementById('end'),
  jump: document.getElementById('jump'),
  poster: document.getElementById('poster'),
  progressbar: document.getElementById('progressbar'),
  ask: document.getElementById('ask'),
  askButton: document.getElementById('ask-button'),
  askResult: document.getElementById('ask-result'),
  pending: document.getElementById('pending'),
  modelLine: document.getElementById('model-line'),
  jevLine: document.getElementById('jev-line'),
  setupIdle: document.getElementById('setup-idle'),
  setupPin: document.getElementById('setup-pin'),
  setupLinked: document.getElementById('setup-linked'),
  setupLink: document.getElementById('setup-link'),
  setupCode: document.getElementById('setup-code'),
  setupStatus: document.getElementById('setup-status'),
  setupError: document.getElementById('setup-error'),
  setupServerLine: document.getElementById('setup-server-line'),
  setupLibraryLine: document.getElementById('setup-library-line'),
  setupReadinessLine: document.getElementById('setup-readiness-line'),
  setupSubtitles: document.getElementById('setup-subtitles'),
  setupSubtitlesHelp: document.getElementById('setup-subtitles-help'),
  setupSubtitlesItems: document.getElementById('setup-subtitles-items'),
  setupSubtitlesResult: document.getElementById('setup-subtitles-result'),
  setupQueue: document.getElementById('setup-queue'),
  setupQueueLine: document.getElementById('setup-queue-line'),
  setupQueueItems: document.getElementById('setup-queue-items'),
  setupStart: document.getElementById('setup-start'),
  setupCancel: document.getElementById('setup-cancel'),
  setupRelink: document.getElementById('setup-relink'),
};

const MODEL_SOURCE_LABELS = {
  override: 'override',
  picker: 'auxiliary picker',
  inherited: 'compression inherit',
  env: 'environment',
  none: '',
};

const state = {
  lastId: null,
  sessionId: null,
  ratingKey: null,
  following: true,
  wakeLock: null,
  timer: null,
  setupTimer: null,
  digestShown: false,
  ask: { id: null, timer: null, phase: 'idle', body: null },
  noteDisplay: 'live',
  pauseRevealed: false,
};

function api(path, options) {
  return fetch(path, Object.assign({ credentials: 'same-origin' }, options || {}))
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status))));
}

function safeUrl(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value, location.origin);
    return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : null;
  } catch (_) {
    return null;
  }
}

function clock(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m + ':' + String(s).padStart(2, '0');
}

function addMessage(message, showSource) {
  const row = document.createElement('article');
  row.className = 'msg';
  row.dataset.id = message.id;
  row.dataset.source = message.source_name || '';
  row.dataset.sourceUrl = message.source_url || '';

  const time = document.createElement('span');
  time.className = 'time';
  time.textContent = clock(message.t_ms);

  const text = document.createElement('p');
  text.className = 'text';
  text.textContent = message.text;           // text only, never parsed as HTML

  const thumb = safeUrl(message.thumb_url);
  if (thumb) {
    const img = document.createElement('img');
    img.className = 'thumb';
    img.src = thumb;
    img.alt = '';
    img.loading = 'lazy';
    row.appendChild(img);
  }
  if (message.model_only) {
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = 'unverified';
    row.appendChild(badge);
  }
  // Pause Reel reveals name their sources inline; the live feed keeps its
  // existing shape and only carries them in the dataset for the digest.
  if (showSource && message.source_name) {
    const sourceUrl = safeUrl(message.source_url);
    const source = document.createElement(sourceUrl ? 'a' : 'span');
    source.className = 'msg-source';
    source.textContent = message.source_name;
    if (sourceUrl) {
      source.href = sourceUrl;
      source.rel = 'noopener noreferrer';
      source.target = '_blank';
    }
    row.appendChild(source);
  }

  row.appendChild(time);
  row.appendChild(text);
  els.stream.appendChild(row);
  if (state.following) els.stream.scrollTop = els.stream.scrollHeight;
}

// -- Pause Reel -------------------------------------------------------------------
//
// note_display comes from the /notes/pending/count payload each poll. In
// 'pause' mode the page stops rendering /messages while playing and shows a
// quiet counter fed by the count endpoint instead. A reveal happens on a
// pause transition or a counter tap: /notes/pending returns the accumulated
// notes (newest group first, marked read server-side) and they are appended
// to the stream like any other note. The counter itself is server-backed, so
// it survives a page refresh.

function revealPending() {
  return api('notes/pending').then((payload) => {
    const notes = payload.notes || [];
    if (!notes.length) return 0;
    const banner = document.createElement('div');
    banner.className = 'reveal-banner';
    banner.textContent = notes.length + (notes.length === 1 ? ' note' : ' notes')
      + ' while the film played';
    els.stream.appendChild(banner);
    notes.forEach((m) => addMessage(m, true));
    state.pauseRevealed = true;
    els.pending.hidden = true;
    if (state.following) els.stream.scrollTop = els.stream.scrollHeight;
    return notes.length;
  });
}

function updatePendingCounter(count, playing) {
  if (state.noteDisplay !== 'pause' || !playing || state.pauseRevealed || count <= 0) {
    els.pending.hidden = true;
    return;
  }
  els.pending.textContent = count + (count === 1 ? ' note waiting' : ' notes waiting');
  els.pending.hidden = false;
}

els.pending.addEventListener('click', () => {
  revealPending().catch(() => {});
});

function renderSession(session) {
  if (!session || session.state === 'idle') {
    els.title.textContent = 'Nothing playing';
    els.subtitle.textContent = 'Start something on Plex.';
    els.status.textContent = session ? session.watcher_state : 'offline';
    els.progressbar.style.width = '0%';
    els.ask.hidden = true;
    els.pending.hidden = true;
    return;
  }
  const media = session.media || {};
  els.title.textContent = media.title || 'Unknown title';
  const parts = [];
  if (session.state) parts.push(session.state);
  if (session.offset_ms) parts.push(clock(session.offset_ms));
  if (media.duration_ms) parts.push(clock(media.duration_ms));
  if (session.player) parts.push(session.player);
  if (session.user) parts.push(session.user);
  if (session.degraded_reason) parts.push(session.degraded_reason);
  els.subtitle.textContent = parts.join(' · ');
  els.status.textContent = session.claimed ? 'live' : 'observed';
  els.mute.textContent = session.muted ? 'Unmute' : 'Mute';
  els.ask.hidden = !(session.claimed && session.projectionist);

  const duration = Number(media.duration_ms || 0);
  const offset = Number(session.offset_ms || 0);
  els.progressbar.style.width = duration
    ? Math.min(100, Math.max(0, (offset / duration) * 100)) + '%'
    : '0%';

  if (session.state === 'ended' && state.sessionId && !state.digestShown) {
    state.digestShown = true;
    showDigest();
  }
}

function showDigest() {
  const seen = new Set();
  const links = [];
  els.stream.querySelectorAll('.msg').forEach((row) => {
    const source = row.dataset.source;
    const url = safeUrl(row.dataset.sourceUrl);
    if (source && url && !seen.has(url)) {
      seen.add(url);
      links.push({ source, url });
    }
  });

  const card = document.createElement('section');
  card.className = 'digest';

  const heading = document.createElement('h2');
  heading.textContent = 'That is the film';
  card.appendChild(heading);

  const line = document.createElement('p');
  const count = els.stream.querySelectorAll('.msg').length;
  line.textContent = count + (count === 1 ? ' note' : ' notes') + ' along the way.';
  card.appendChild(line);

  if (links.length) {
    const list = document.createElement('ul');
    links.slice(0, 5).forEach(({ source, url }) => {
      const item = document.createElement('li');
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.textContent = source;
      anchor.rel = 'noopener noreferrer';
      anchor.target = '_blank';
      item.appendChild(anchor);
      list.appendChild(item);
    });
    card.appendChild(list);
  }

  els.stream.appendChild(card);
  if (state.following) els.stream.scrollTop = els.stream.scrollHeight;

  if (state.ratingKey) {
    api('credits?rating_key=' + encodeURIComponent(state.ratingKey))
      .then(renderCredits)
      .catch(() => {});
  }
}

function renderCredits(payload) {
  const claims = (payload && payload.claims) || [];
  if (!claims.length) return;

  const card = document.createElement('section');
  card.className = 'credits';

  const heading = document.createElement('h2');
  heading.textContent = "What we couldn't tell you yet";
  card.appendChild(heading);

  const note = document.createElement('p');
  note.className = 'credits-note';
  note.textContent = 'Held back so the film could surprise you:';
  card.appendChild(note);

  const list = document.createElement('ul');
  claims.forEach((claim) => {
    const item = document.createElement('li');

    const text = document.createElement('span');
    text.textContent = claim.text;
    item.appendChild(text);

    const when = document.createElement('span');
    when.className = 'credits-when';
    if (claim.reveal_state === 'locked' && claim.unlock_anchor_ms != null) {
      when.textContent = ' — unlocks at ' + clock(claim.unlock_anchor_ms);
    } else {
      when.textContent = ' — hold until the credits';
    }
    item.appendChild(when);

    const sourceUrl = safeUrl(claim.source_url);
    if (claim.source_name && sourceUrl) {
      const source = document.createElement('a');
      source.href = sourceUrl;
      source.textContent = ' [' + claim.source_name + ']';
      source.rel = 'noopener noreferrer';
      source.target = '_blank';
      item.appendChild(source);
    } else if (claim.source_name) {
      const source = document.createElement('span');
      source.className = 'credits-when';
      source.textContent = ' [' + claim.source_name + ']';
      item.appendChild(source);
    }

    list.appendChild(item);
  });
  card.appendChild(list);

  els.stream.appendChild(card);
  if (state.following) els.stream.scrollTop = els.stream.scrollHeight;
}

function renderJev(jev) {
  // One line, no badge, no nag: off (or absent entirely) reads as the plain
  // optional offer; on reports only the resolved provider and the key's
  // last four (the payload never carries the key itself).
  if (!jev || !jev.enabled) {
    els.jevLine.textContent = 'Optional: use a decision model to choose which entities are worth expanding';
    return;
  }
  const provider = jev.provider || 'auto';
  const key = jev.configured ? ' (key ····' + jev.last4 + ')' : '';
  els.jevLine.textContent = 'Entity selection: Jev via ' + provider + key;
}

function renderSettings(payload) {
  const model = payload.model || 'not configured';
  const via = MODEL_SOURCE_LABELS[payload.model_source] || '';
  const source = via ? ' (via ' + via + ')' : '';
  els.modelLine.textContent = 'Notes model: ' + model + source + ' — ' + payload.guidance;
  renderJev(payload.jev);
}

function loadSettings() {
  api('settings').then(renderSettings).catch(() => {
    els.modelLine.textContent = 'Notes model: unavailable';
  });
}

// -- Plex onboarding -----------------------------------------------------------

const SETUP_POLL_MS = 2000;

function showPane(which) {
  els.setupIdle.hidden = which !== 'idle';
  els.setupPin.hidden = which !== 'pin';
  els.setupLinked.hidden = which !== 'linked';
}

function renderSetup(payload) {
  const phase = payload.phase || 'idle';
  if (phase === 'awaiting_link') {
    showPane('pin');
    if (payload.code) els.setupCode.textContent = payload.code;
    els.setupStatus.textContent = payload.code
      ? 'Waiting for you to link…'
      : 'Requesting a link code…';
    scheduleSetupPoll();
    return;
  }
  if (phase === 'working') {
    showPane('pin');
    if (payload.code) els.setupCode.textContent = payload.code;
    els.setupStatus.textContent = 'Linked — finding your server and checking libraries…';
    scheduleSetupPoll();
    return;
  }
  clearTimeout(state.setupTimer);
  state.setupTimer = null;
  const server = payload.server;
  if (server) {
    showPane('linked');
    const relay = server.relay_fallback ? ' — relay fallback (slow)' : '';
    els.setupServerLine.textContent =
      'Server: ' + server.name + ' (' + (server.access || 'your server') + ')' + relay;
    if (server.library_count != null) {
      const names = (server.library_names || []).join(', ');
      els.setupLibraryLine.textContent =
        'Libraries: ' + server.library_count + (names ? ' — ' + names : '');
      els.setupLibraryLine.hidden = false;
    } else {
      els.setupLibraryLine.hidden = true;
    }
    if (server.readiness && server.readiness.note) {
      els.setupReadinessLine.textContent = 'Readiness: ' + server.readiness.note;
      els.setupReadinessLine.hidden = false;
    } else {
      els.setupReadinessLine.hidden = true;
    }
    renderSubtitles(server);
  } else {
    showPane('idle');
  }
  if (payload.error) {
    els.setupError.textContent = payload.error;
    els.setupError.hidden = false;
  } else {
    els.setupError.hidden = true;
  }
}

// States that can be fixed by downloading a text subtitle (the per-item action
// below). Mirrors watch_with_me/subtitles.py and the RESEARCH-PREPARATION.md
// §12 taxonomy.
const SUBTITLE_ACTION_STATES = { none: 1, image_only: 1, embedded: 1 };

function renderSubtitles(server) {
  const download = server.download;
  const items = server.readiness && server.readiness.items;
  if (!download || !items || !items.length) {
    els.setupSubtitles.hidden = true;
    return;
  }
  els.setupSubtitles.hidden = false;
  els.setupSubtitlesHelp.textContent = download.help
    + (download.enabled
      ? ' Automatic download is ON (up to ' + download.budget
        + ' per run, languages: ' + (download.languages || []).join(', ') + ').'
      : ' Automatic download is off — the buttons below are the per-item way to do it.');
  els.setupSubtitlesItems.replaceChildren();
  items.forEach((item) => {
    const row = document.createElement('li');
    row.className = 'setup-item';

    const label = document.createElement('span');
    label.textContent = item.title
      + (item.state === 'sidecar' ? '' : ' — ' + (item.message || item.state));
    row.appendChild(label);

    const badge = document.createElement('span');
    badge.className = 'badge badge-' + item.state;
    badge.textContent = item.state;
    row.appendChild(badge);

    if (SUBTITLE_ACTION_STATES[item.state]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = 'Download subtitle';
      button.addEventListener('click', () => {
        button.disabled = true;
        els.setupSubtitlesResult.hidden = true;
        api('setup/subtitles/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rating_key: item.rating_key }),
        }).then((result) => {
          els.setupSubtitlesResult.textContent = result.message
            || (result.state === 'downloaded'
              ? 'Downloaded and cached (' + (result.cues || 0) + ' cues).'
              : result.state);
          els.setupSubtitlesResult.hidden = false;
          loadSetup();  // re-classify the item list
        }).catch(() => {
          els.setupSubtitlesResult.textContent = 'Download failed — try again.';
          els.setupSubtitlesResult.hidden = false;
          button.disabled = false;
        });
      });
      row.appendChild(button);
    }
    els.setupSubtitlesItems.appendChild(row);
  });
}

function scheduleSetupPoll() {
  if (state.setupTimer) return;
  state.setupTimer = setTimeout(loadSetup, SETUP_POLL_MS);
}

// The preparation view: one line per Continue Watching item. States come
// straight from the store; "unsupported" renders as "will not work" with the
// taxonomy remedy as the reason.
function renderQueue(payload) {
  const box = els.setupQueue;
  if (!payload || !payload.items || !payload.items.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const budget = payload.budget || {};
  let line = 'Continue Watching preparation: ' + budget.used + ' of ' + budget.limit
    + ' builds used today';
  if (payload.depth && payload.depth !== 'off') {
    line += ' (depth: ' + payload.depth + ')';
  } else {
    line += ' (preparation is off — set prepare_depth to enable)';
  }
  if (!payload.watcher_running) {
    line += ' — watcher is not running';
  }
  els.setupQueueLine.textContent = line;
  els.setupQueueItems.replaceChildren();
  payload.items.forEach((item) => {
    const row = document.createElement('li');
    row.className = 'setup-item';

    const label = document.createElement('span');
    label.textContent = item.title + (item.detail ? ' — ' + item.detail : '');
    row.appendChild(label);

    const badge = document.createElement('span');
    const stateName = item.state === 'unsupported' ? 'will not work' : item.state;
    badge.className = 'badge badge-' + item.state;
    badge.textContent = stateName;
    row.appendChild(badge);

    els.setupQueueItems.appendChild(row);
  });
}

function loadQueue() {
  api('queue').then(renderQueue).catch(() => {});
}

function loadSetup() {
  state.setupTimer = null;
  api('setup').then((payload) => {
    renderSetup(payload);
    if (payload.linked) loadQueue();
  }).catch(() => {
    els.setupTimer = setTimeout(loadSetup, 8000);
  });
}

els.setupStart.addEventListener('click', () => {
  api('setup/pin', { method: 'POST' }).then(renderSetup).catch(() => {});
});

els.setupRelink.addEventListener('click', () => {
  api('setup/relink', { method: 'POST' }).then(renderSetup).catch(() => {});
});

els.setupCancel.addEventListener('click', () => {
  api('setup/cancel', { method: 'POST' }).then(loadSetup).catch(() => {});
});

// -- Ask the Projectionist -------------------------------------------------------
//
// One button: "What did I just see?" Tapping POSTs /ask, which returns an
// ask_id immediately; we poll /ask/{id} while the server assembles the
// answer. A second tap supersedes the first ask server-side.

const ASK_POLL_MS = 1500;

function askReset() {
  clearTimeout(state.ask.timer);
  state.ask = { id: null, timer: null, phase: 'idle', body: null };
  els.askButton.disabled = false;
  els.askButton.classList.remove('asking');
  els.askButton.textContent = 'What did I just see?';
  els.askResult.hidden = true;
  els.askResult.className = '';
  els.askResult.replaceChildren();
}

function askSetPhase(phase, body) {
  state.ask.phase = phase;
  state.ask.body = body || null;
  els.askButton.classList.toggle('asking', phase === 'asking');
  if (phase === 'asking') {
    els.askButton.disabled = true;
    els.askButton.textContent = '';
    const spinner = document.createElement('span');
    spinner.className = 'ask-spinner';
    spinner.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.textContent = 'looking…';
    els.askButton.appendChild(spinner);
    els.askButton.appendChild(label);
  } else {
    els.askButton.disabled = false;
    els.askButton.textContent = 'What did I just see?';
  }
  renderAskResult();
}

function renderAskResult() {
  els.askResult.replaceChildren();
  els.askResult.className = '';
  const body = state.ask.body;
  if (state.ask.phase === 'answered' && body) {
    const card = document.createElement('article');
    card.className = 'ask-card';
    const text = document.createElement('p');
    text.textContent = body.answer;
    card.appendChild(text);
    const sources = (body.sources || []).filter((s) => s && s.source_url);
    if (sources.length) {
      const line = document.createElement('p');
      line.className = 'ask-sources';
      line.textContent = 'source: ';
      sources.forEach((s, index) => {
        if (index) line.appendChild(document.createTextNode(' · '));
        const link = document.createElement('a');
        link.href = s.source_url;
        link.textContent = s.source_name || s.fact_id;
        link.rel = 'noopener noreferrer';
        link.target = '_blank';
        line.appendChild(link);
      });
      card.appendChild(line);
    }
    els.askResult.appendChild(card);
    els.askResult.hidden = false;
  } else if (state.ask.phase === 'nothing') {
    els.askResult.textContent = 'Nothing solid on that one.';
    els.askResult.className = 'ask-nothing';
    els.askResult.hidden = false;
  } else {
    els.askResult.hidden = true;
  }
}

function pollAsk() {
  if (!state.ask.id) return;
  api('ask/' + encodeURIComponent(state.ask.id)).then((body) => {
    if (body.status === 'asking') {
      state.ask.timer = setTimeout(pollAsk, ASK_POLL_MS);
      return;
    }
    state.ask.id = null;
    if (body.status === 'answered') askSetPhase('answered', body);
    else if (body.status === 'nothing') askSetPhase('nothing', body);
    else askSetPhase('idle', null); // superseded by a newer tap
  }).catch(() => {
    state.ask.timer = setTimeout(pollAsk, 6000);
  });
}

els.askButton.addEventListener('click', () => {
  api('ask', { method: 'POST' }).then((payload) => {
    clearTimeout(state.ask.timer);
    state.ask.id = payload.ask_id;
    askSetPhase('asking');
    state.ask.timer = setTimeout(pollAsk, ASK_POLL_MS);
  }).catch(() => {
    askSetPhase('idle', null);
  });
});

function poll() {
  api('session').then((session) => {
    renderSession(session);
    const claimed = session && session.claimed && session.session_id;
    if (!claimed) {
      els.pending.hidden = true;
      schedule(10000);
      return;
    }
    // Plex reuses a player's session id across titles, so a new item must
    // reset the stream even when the session id is unchanged.
    const currentKey = session.media ? session.media.rating_key : null;
    if (session.session_id !== state.sessionId
        || currentKey !== state.ratingKey) {
      state.sessionId = session.session_id;
      state.ratingKey = currentKey;
      state.lastId = null;
      state.digestShown = false;
      state.pauseRevealed = false;
      els.stream.replaceChildren();
      els.pending.hidden = true;
      askReset();
    }
    // One small round trip carries both the waiting count and the display
    // mode; the mode decides whether this poll renders the live feed.
    return api('notes/pending/count').then((pending) => {
      state.noteDisplay = pending.note_display || 'live';
      if (state.noteDisplay !== 'pause') {
        els.pending.hidden = true;
        state.pauseRevealed = false;
        return pollMessages(session);
      }
      // Pause Reel: delivery continues server-side, but nothing renders
      // while playing. A paused session reveals the accumulated group once;
      // the counter (or the next pause) handles the rest.
      const paused = session.state === 'paused';
      if (paused && !state.pauseRevealed) {
        return revealPending().catch(() => {}).then(() => schedule(8000));
      }
      if (!paused && state.pauseRevealed) {
        // The group has been read; the counter may return for new notes.
        state.pauseRevealed = false;
      }
      updatePendingCounter(pending.count, !paused);
      schedule(paused ? 8000 : 3000);
    });
  }).catch(() => {
    els.status.textContent = 'reconnecting';
    schedule(8000);
  });
}

function pollMessages(session) {
  const query = state.lastId ? '?after_id=' + encodeURIComponent(state.lastId) : '';
  return api('messages' + query).then((payload) => {
    (payload.messages || []).forEach((m) => {
      addMessage(m);
      state.lastId = m.id;
    });
    schedule(session.state === 'paused' ? 8000 : 3000);
  });
}

function schedule(delay) {
  clearTimeout(state.timer);
  state.timer = setTimeout(poll, delay);
}

els.stream.addEventListener('scroll', () => {
  const nearBottom = els.stream.scrollHeight - els.stream.scrollTop - els.stream.clientHeight < 60;
  state.following = nearBottom;
  els.jump.hidden = nearBottom;
});

els.jump.addEventListener('click', () => {
  state.following = true;
  els.stream.scrollTop = els.stream.scrollHeight;
  els.jump.hidden = true;
});

els.mute.addEventListener('click', () => {
  const muted = els.mute.textContent === 'Mute' ? 'mute' : 'unmute';
  api('control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: muted }),
  }).then(poll);
});

els.end.addEventListener('click', () => {
  api('control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'end' }),
  }).then(poll);
});

els.wake.addEventListener('click', async () => {
  if (!('wakeLock' in navigator)) {
    els.wake.textContent = 'Unsupported';
    return;
  }
  try {
    if (state.wakeLock) {
      await state.wakeLock.release();
      state.wakeLock = null;
      els.wake.setAttribute('aria-pressed', 'false');
      els.wake.textContent = 'Keep awake';
      return;
    }
    state.wakeLock = await navigator.wakeLock.request('screen');
    state.wakeLock.addEventListener('release', () => { state.wakeLock = null; });
    els.wake.setAttribute('aria-pressed', 'true');
    els.wake.textContent = 'Awake';
  } catch (_) {
    els.wake.textContent = 'Denied';
  }
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    poll();
    loadSettings();
    loadSetup();
  }
});

loadSettings();
loadSetup();
poll();
