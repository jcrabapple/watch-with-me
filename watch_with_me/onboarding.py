"""Plex account onboarding: PIN link, server discovery, probe, verified save.

The user never sees a Plex token. The flow is:

1. ``create_pin`` posts to plex.tv and returns a four-digit code the user types
   at https://plex.tv/link on any device.
2. ``poll_pin_token`` polls the pin until plex.tv attaches an ``authToken``.
3. ``filter_server_resources`` + ``choose_connection`` find a server that
   actually answers (plex.tv's ``local`` flag is unreliable: measured on a real
   install, 9 of 11 ``local: true`` URIs were docker bridges that do not
   respond), preferring a non-relay connection.
4. ``verify_server`` checks ``/library/sections`` and classifies a readiness
   sample of ten recent items, so the user learns whether their library can
   produce notes at all.
5. Only then does ``save_credentials`` write the token to disk (mode 0600).

Security contract: the token appears only in the credentials file and in
``X-Plex-Token`` request headers — never in a URL (never logged), never in a
``PlexError`` message (those carry paths, which carry no token), and never in
any payload returned to the dashboard or the CLI.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import store
from .plex import PlexClient, PlexError, classify_preparation

log = logging.getLogger(__name__)

PLEX_TV = "https://plex.tv"
LINK_URL = "https://plex.tv/link"
PRODUCT = "watch-with-me"
VERSION = "1.1.0"

CREDENTIALS = "plex-credentials.json"
CLIENT_ID_FILE = "plex-client-id"

PIN_TIMEOUT_S = 300.0       # hard cap on how long we wait for the user to link
PIN_POLL_INTERVAL_S = 2.0
PROBE_TIMEOUT_S = 2.0
READINESS_SAMPLE_SIZE = 10

#: User-facing remedy repeated wherever sidecar coverage is reported.
SIDECAR_REMEDY = "download subtitles in Plex, or set an extract command"

# Token rejected by plex.tv or the server: the saved token is stale or the
# user revoked access. The only fix is a fresh link, and the page must say so.
RELINK_MESSAGE = "Plex rejected the token — relink your account"


def _http_json(method: str, url: str, headers: dict | None = None,
               timeout: float = 10.0, transport=None) -> dict:
    """One JSON HTTP call; ``transport`` is the test seam.

    ``transport(method, url, headers, timeout)`` returns a parsed dict and
    raises ``urllib.error.HTTPError`` / ``urllib.error.URLError`` like the real
    stack, so the fake and the real path fail identically.
    """
    if transport is not None:
        try:
            return transport(method, url, dict(headers or {}), timeout)
        except urllib.error.HTTPError as exc:
            raise PlexError(f"Plex HTTP {exc.code} for {url}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise PlexError(f"Plex unreachable: {exc.reason}") from exc
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise PlexError(f"Plex HTTP {exc.code} for {url}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise PlexError(f"Plex unreachable: {exc.reason}") from exc
    return json.loads(body or b"{}")


def plex_tv_headers(client_id: str, token: str | None = None) -> dict:
    headers = {
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": VERSION,
        "X-Plex-Client-Identifier": client_id,
        "Accept": "application/json",
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def client_identifier() -> str:
    """The X-Plex-Client-Identifier for this install: generated once, persisted.

    Plex rate-limits and de-duplicates by client identifier, so it must be
    stable per install — never the default that changes with the version.
    """
    path = store.data_dir() / CLIENT_ID_FILE
    if path.exists():
        value = path.read_text("utf-8").strip()
        if value:
            return value
    value = str(uuid.uuid4())
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)
    return value


# -- credentials storage (the one writer) -------------------------------------

def credentials_path() -> Path:
    return store.data_dir() / CREDENTIALS


def save_credentials(*, base_url: str, token: str, client_id: str,
                     server_name: str = "", machine_id: str = "",
                     username: str = "", account_title: str = "",
                     path: Path | None = None) -> Path:
    """Write the credentials file (mode 0600). The single writer for the file.

    ``relink`` is just this function again: the file is replaced wholesale.
    ``username`` is the linked Plex account's username, captured during the
    PIN link flow; it is persisted only when known, so files written by older
    versions (and callers that never pass it) keep their exact shape.
    ``account_title`` is the Plex display title (``User.title`` on sessions);
    Plex Home users may differ from the username, so both are stored and both
    authorize when they differ.
    """
    target = path or credentials_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "base_url": base_url,
        "token": token,
        "client_id": client_id,
        "server_name": server_name,
        "machine_id": machine_id,
    }
    if username:
        body["username"] = username
    if account_title and account_title != username:
        body["account_title"] = account_title
    target.write_text(json.dumps(body), encoding="utf-8")
    os.chmod(target, 0o600)
    os.chmod(target.parent, 0o700)
    return target


def load_credentials(path: Path | None = None) -> dict | None:
    target = path or credentials_path()
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text("utf-8"))
    except (OSError, ValueError):
        log.warning("watch-with-me: unreadable credential file")
        return None


# -- step 1: account link via PIN ----------------------------------------------

def create_pin(client_id: str, *, transport=None) -> dict:
    """Create a link PIN; returns ``{"id", "code", "expires_at"}``.

    ``strong=false`` is deliberate: a strong PIN returns a short-lived JWT,
    which is wrong for a token that is stored and reused for months. Do not
    "improve" this to strong=true — the JWT would expire and every Plex call
    would start failing with 401 after a few minutes.
    """
    payload = _http_json(
        "POST", f"{PLEX_TV}/api/v2/pins?strong=false",
        headers=plex_tv_headers(client_id), timeout=15.0, transport=transport)
    return {
        "id": str(payload["id"]),
        "code": str(payload["code"]),
        "expires_at": int(payload.get("expiresAt") or 0),
    }


def poll_pin_token(pin_id: str, client_id: str, *, transport=None,
                   timeout_s: float = PIN_TIMEOUT_S,
                   interval_s: float = PIN_POLL_INTERVAL_S,
                   sleep=None, monotonic=None) -> str:
    """Poll the pin until plex.tv attaches ``authToken``; raise on timeout."""
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    deadline = monotonic() + timeout_s
    while True:
        sleep(interval_s)
        state = _http_json("GET", f"{PLEX_TV}/api/v2/pins/{pin_id}",
                           headers=plex_tv_headers(client_id), timeout=15.0,
                           transport=transport)
        token = state.get("authToken")
        if token:
            return str(token)
        if monotonic() >= deadline:
            raise PlexError("Timed out waiting for the Plex link "
                            f"(code expired after {int(timeout_s // 60)} minutes)")


# -- step 2: server discovery ---------------------------------------------------

def fetch_account_identity(token: str, client_id: str, *, transport=None) -> dict:
    """The linked account's ``{"username", "title"}``; empty strings on failure.

    Plex Home users often have a display title (what sessions report as
    ``User.title``) that differs from the account username — both are
    captured so zero-config authorization matches either. A transient
    plex.tv failure must never abort linking, so every failure mode (HTTP
    error, timeout, malformed payload) returns empties.
    """
    try:
        payload = _http_json("GET", f"{PLEX_TV}/api/v2/user",
                             headers=plex_tv_headers(client_id, token),
                             timeout=10.0, transport=transport)
    except Exception:  # noqa: BLE001 - the identity is an enhancement, never a gate
        return {"username": "", "title": ""}
    if not isinstance(payload, dict):
        return {"username": "", "title": ""}
    return {"username": str(payload.get("username") or ""),
            "title": str(payload.get("title") or "")}


def fetch_account_user(token: str, client_id: str, *, transport=None) -> str:
    """The linked Plex account's username (or title); ``""`` on any failure.

    Called right after the PIN link attaches a token so the watcher can treat
    the account owner as eligible when ``allowed_user_ids`` is empty. A
    transient plex.tv failure must never abort linking, so every failure
    mode (HTTP error, timeout, malformed payload) returns ``""``.
    """
    identity = fetch_account_identity(token, client_id, transport=transport)
    return identity["username"] or identity["title"]


def fetch_resources(token: str, client_id: str, *, transport=None) -> list[dict]:
    payload = _http_json(
        "GET", f"{PLEX_TV}/api/v2/resources?includeHttps=1&includeRelay=1",
        headers=plex_tv_headers(client_id, token), timeout=20.0, transport=transport)
    return payload if isinstance(payload, list) else []


def filter_server_resources(payload: list[dict]) -> list[dict]:
    """Keep entries that provide ``server``; owned and shared both survive.

    ``provides`` arrives as a comma string ("server,player,sync"); anything
    without ``server`` in it (companion players, synced devices) is dropped.
    """
    servers = []
    for entry in payload:
        provides = [p.strip() for p in str(entry.get("provides") or "").split(",")]
        if "server" not in provides:
            continue
        servers.append({
            "name": entry.get("name") or "Unknown server",
            "machine_id": entry.get("clientIdentifier") or "",
            "owned": bool(entry.get("owned")),
            "access": "your server" if entry.get("owned") else "shared with you",
            "connections": [
                {"uri": (c.get("uri") or "").rstrip("/"), "local": bool(c.get("local")),
                 "relay": bool(c.get("relay"))}
                for c in entry.get("connections", []) or [] if c.get("uri")
            ],
        })
    return servers


def probe_connection(uri: str, token: str, client_id: str, *,
                     timeout: float = PROBE_TIMEOUT_S, transport=None) -> bool:
    """True when the URI answers GET /identity with 200.

    Every advertised URI is probed: ``local`` is a claim, not a fact — docker
    bridge addresses advertise ``local: true`` and never answer.
    """
    try:
        _http_json("GET", f"{uri}/identity",
                   headers=plex_tv_headers(client_id, token), timeout=timeout,
                   transport=transport)
        return True
    except PlexError as exc:
        # A 401 means the token itself is stale — surface it so the flow can
        # say "relink" instead of the weaker "no reachable server".
        if exc.status == 401:
            raise
        return False
    except Exception:  # noqa: BLE001 - a dead URI is data, not an error
        return False


def choose_connection(connections: list[dict], token: str, client_id: str, *,
                      transport=None) -> dict | None:
    """Pick the connection to use.

    Probes every URI and prefers a non-relay answering connection. Relay is
    only ever chosen when nothing else answers, and is flagged
    ``relay_fallback`` because relay playback-adjacent traffic is slow.

    Returns ``{"uri", "relay", "relay_fallback", "answering"}`` or None when
    no connection answers at all.
    """
    answering = [c for c in connections
                 if probe_connection(c["uri"], token, client_id, transport=transport)]
    if not answering:
        return None
    non_relay = [c for c in answering if not c.get("relay")]
    chosen = non_relay[0] if non_relay else answering[0]
    return {
        "uri": chosen["uri"],
        "relay": bool(chosen.get("relay")),
        "relay_fallback": not non_relay,
        "answering": [c["uri"] for c in answering],
    }


def choose_server(servers: list[dict], token: str, client_id: str, *,
                  transport=None) -> dict:
    """Pick the server to use and the connection that reaches it.

    Owned servers win over shared ones; within a server, ``choose_connection``
    decides. Raises ``PlexError`` when nothing answers.
    """
    ordered = sorted(servers, key=lambda s: not s["owned"])
    failures = []
    for server in ordered:
        choice = choose_connection(server["connections"], token, client_id,
                                   transport=transport)
        if choice is None:
            failures.append(server["name"])
            continue
        return {"server": server, **choice}
    detail = f" (none of {', '.join(failures)} answered)" if failures else ""
    raise PlexError(f"No reachable Plex server found{detail}")


# -- step 3: verify before saving ------------------------------------------------

def readiness_sample(client: PlexClient, limit: int = READINESS_SAMPLE_SIZE) -> dict:
    """Classify ten recent *playable* items and report subtitle coverage.

    This is the step that tells the user whether their library will produce
    timed notes at all. ``/library/recently_added`` returns shows and seasons
    (not playable items) and omits ``Stream`` — classifying its rows directly
    reported ``none`` for items that demonstrably have fetchable sidecars
    (RESEARCH-PREPARATION.md §13.3). So candidates are first resolved to
    ``type in {"movie", "episode"}`` and then re-fetched with
    ``GET /library/metadata/{ratingKey}`` (~25 ms each) before classification.

    Detection never fails the sample: an item whose metadata cannot be fetched
    classifies as ``unknown`` rather than raising — an empty state is a
    library truth, not a plugin error.
    """
    # Over-fetch because shows/seasons occupy listing rows; the extra
    # candidates are discarded after the playable filter.
    candidates = [
        item for item in client.recently_added(limit=limit * 3)
        if item.get("type") in ("movie", "episode")
    ][:limit]
    classified = []
    for item in candidates:
        rating_key = str(item.get("ratingKey") or "")
        try:
            metadata = client.metadata(rating_key)
        except PlexError:
            metadata = {}
        result = classify_preparation(metadata)
        classified.append({
            "rating_key": rating_key,
            "title": item.get("title"),
            "type": item.get("type"),
            "year": item.get("year"),
            "grandparent_title": item.get("grandparentTitle"),
            **result,
        })
    with_sidecar = sum(1 for c in classified if c["state"] == "sidecar")
    return {"sampled": len(classified), "with_sidecar": with_sidecar,
            "items": classified, "note": readiness_note(len(classified), with_sidecar)}


def readiness_note(sampled: int, with_sidecar: int) -> str:
    if sampled == 0:
        return "No recent items found — add something to the library and retry."
    missing = sampled - with_sidecar
    if missing == 0:
        return f"All {sampled} sampled titles have sidecar subtitles — full pipeline ready."
    return (f"{missing} of {sampled} sampled titles have no subtitle file — "
            f"{SIDECAR_REMEDY}.")


def verify_server(base_url: str, token: str, client_id: str, *,
                  allow_loopback: bool = False) -> dict:
    """Verify a chosen server: sections plus the readiness sample.

    Raises ``PlexError`` (401 maps to a relink message via ``user_message``)
    when the server cannot be used — the caller must not save credentials then.
    """
    client = PlexClient(base_url, token, client_id, allow_loopback=allow_loopback)
    sections = client.library_sections()
    sample = readiness_sample(client)
    return {
        "library_count": len(sections),
        "library_names": [s.get("title") or "unknown" for s in sections],
        "readiness": sample,
    }


# -- error surfacing ---------------------------------------------------------------

def user_message(exc: BaseException) -> str:
    """The only string about an error that may reach the page or the chat.

    Never includes the token: ``PlexError`` messages carry paths only, and the
    token travels in headers, so nothing here can leak it.
    """
    if isinstance(exc, PlexError):
        if exc.status == 401:
            return RELINK_MESSAGE
        return f"Plex error: {exc}"
    return f"Unexpected error ({type(exc).__name__}): {exc}"
