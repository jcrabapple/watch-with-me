"""Read-only Plex HTTP client.

Contract names come from RESEARCH.md, not memory:
  GET /status/sessions
  GET /library/metadata/<key>?includeGuids=1&includeChapters=1
  GET /hubs/continueWatching
  GET /library/streams/<id>.<ext>?encoding=utf-8
"""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 10.0
BLOCKED_V4 = [ipaddress.ip_network("169.254.169.254/32"), ipaddress.ip_network("0.0.0.0/32")]
TEXT_SUB_CODECS = {"srt", "ass", "ssa", "mov_text", "subrip"}
#: Picture-based subtitle codecs from RESEARCH-PREPARATION.md §12 row 3. These
#: carry no text; OCR is not supported, so they classify as image_only.
IMAGE_SUB_CODECS = {"pgs", "vobsub", "dvd_subtitle", "hdmv_pgs_subtitle"}


class PlexError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _is_blocked(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True
    return any(ip in net for net in BLOCKED_V4 if net.version == ip.version)


def validate_base_url(url: str, allow_loopback: bool = False) -> str:
    """Reject schemes and destinations the watcher must never poll.

    Private LAN ranges are allowed: that is where Plex lives. Loopback is
    opt-in for development; metadata and link-local are always refused.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise PlexError(f"unsupported scheme {parsed.scheme!r}")
    if not parsed.hostname:
        raise PlexError("missing host")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    for info in infos:
        addr = info[4][0]
        ip = ipaddress.ip_address(addr)
        if ip.is_loopback and not allow_loopback:
            raise PlexError("loopback destination refused (set allow_loopback_plex for development)")
        if ip.is_link_local or ip in BLOCKED_V4[0]:
            raise PlexError("link-local or metadata destination refused")
        if ip.is_multicast or ip.is_unspecified:
            raise PlexError("multicast or unspecified destination refused")
    return parsed.geturl().rstrip("/")


class PlexClient:
    def __init__(self, base_url: str, token: str, client_id: str, *, allow_loopback: bool = False,
                 timeout: float = DEFAULT_TIMEOUT, transport=None) -> None:
        self.base_url = validate_base_url(base_url, allow_loopback=allow_loopback)
        self.token = token
        self.client_id = client_id
        self.timeout = timeout
        # Test seam: transport(method, url, headers, timeout, *, raw) returns a
        # parsed dict, or bytes when raw=True, and raises the same
        # urllib.error exceptions the real stack raises.
        self._transport = transport

    # -- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, *, accept: str = "application/json",
                 raw: bool = False):
        url = f"{self.base_url}{path}"
        headers = {
            "X-Plex-Token": self.token,
            "X-Plex-Client-Identifier": self.client_id,
            "Accept": accept,
        }
        if self._transport is not None:
            try:
                return self._transport(method, url, headers, self.timeout, raw=raw)
            except urllib.error.HTTPError as exc:  # noqa: PERF203 - explicit status contract
                raise PlexError(f"Plex HTTP {exc.code} for {path}", status=exc.code) from exc
            except urllib.error.URLError as exc:
                raise PlexError(f"Plex unreachable: {exc.reason}") from exc
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:  # noqa: PERF203 - explicit status contract
            raise PlexError(f"Plex HTTP {exc.code} for {path}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise PlexError(f"Plex unreachable: {exc.reason}") from exc
        if raw:
            return body
        return json.loads(body or b"{}")

    def _get(self, path: str, *, accept: str = "application/json", raw: bool = False):
        return self._request("GET", path, accept=accept, raw=raw)

    # -- reads -------------------------------------------------------------
    def sessions(self) -> list[dict]:
        payload = self._get("/status/sessions")
        items = payload.get("MediaContainer", {}).get("Metadata", []) or []
        return [normalise_session(md) for md in items]

    def metadata(self, rating_key: str) -> dict:
        payload = self._get(f"/library/metadata/{rating_key}?includeGuids=1&includeChapters=1")
        items = payload.get("MediaContainer", {}).get("Metadata", []) or []
        return items[0] if items else {}

    def continue_watching(self) -> list[dict]:
        """The Continue Watching queue, normalised, in viewing order.

        The endpoint answers one of two shapes (both verified live,
        RESEARCH-PREPARATION.md §9.1):

        - ``GET /hubs/continueWatching`` nests the queue under
          ``MediaContainer.Hub[0].Metadata`` (a Hub, not a flat list).
        - ``GET /hubs/continueWatching/items`` paged returns a flat
          ``MediaContainer.Metadata``.

        Both are parsed; the returned dicts use the normalised field names.
        """
        payload = self._get("/hubs/continueWatching")
        container = payload.get("MediaContainer", {}) or {}
        items = container.get("Metadata") or []
        if not items:
            for hub in container.get("Hub") or []:
                items = hub.get("Metadata") or []
                if items:
                    break
        return [normalise_continue_watching_item(md) for md in items]

    def show_leaves(self, rating_key: str) -> list[dict]:
        """Every episode leaf of a show, in air order (``(parentIndex, index)``).

        One request per show (RESEARCH-PREPARATION.md §9.3, ~0.10 s) used to
        expand a Continue Watching item to the following episodes.
        """
        payload = self._get(f"/library/metadata/{rating_key}/allLeaves")
        items = payload.get("MediaContainer", {}).get("Metadata", []) or []
        return sorted(items, key=lambda m: (int(m.get("parentIndex") or 0),
                                            int(m.get("index") or 0)))

    def sidecar(self, stream_key: str, extension: str = "srt") -> str:
        """Fetch a sidecar subtitle as text. Raises PlexError(501) for embedded streams."""
        key = stream_key.rstrip("/")
        if not key.endswith(f".{extension}"):
            key = f"{key}.{extension}"
        return self._get(f"{key}?encoding=utf-8", accept="text/plain", raw=True).decode(
            "utf-8", "replace"
        )

    # -- subtitle search-and-attach (RESEARCH-PREPARATION.md §4) --------------
    def search_subtitles(self, rating_key: str, language: str, *, forced: int = 0,
                         hearing_impaired: int = 0) -> list[dict]:
        """On-demand subtitle search (read-only). Returns the provider's candidate
        ``Stream`` list; an empty list is a real answer (``none``), not an error."""
        payload = self._get(
            f"/library/metadata/{rating_key}/subtitles?language={urllib.parse.quote(language)}"
            f"&forced={forced}&hearingImpaired={hearing_impaired}"
        )
        return payload.get("MediaContainer", {}).get("Stream", []) or []

    def attach_subtitle(self, rating_key: str, key: str, codec: str = "srt") -> None:
        """Attach a search result to the item — the one mutating call in the client.

        ``key`` is the candidate's ``/library/streams/{id}`` path; it is sent
        URL-encoded, matching what Plex Web itself sends (§4.2). Raises
        ``PlexError`` with ``status`` on non-2xx; the caller decides the message.
        """
        query = urllib.parse.urlencode({"key": key, "codec": codec})
        self._request("PUT", f"/library/metadata/{rating_key}/subtitles?{query}")

    def library_sections(self) -> list[dict]:
        payload = self._get("/library/sections")
        return payload.get("MediaContainer", {}).get("Directory", []) or []

    # -- frames (read-only thumbnails; no library mutation) ----------------------
    def timeline_probe(self, rating_key: str, ms_a: int = 60_000,
                       ms_b: int = 1_800_000) -> bool:
        """Does ``/thumb/{ms}`` serve real per-timestamp frames on this server?

        Plex without generated video preview thumbnails silently returns the
        item poster for any ``ms``; feeding that to a vision model as "the
        current frame" would be a broken call. This probe fetches two distant
        timestamps and requires distinct bytes. Measured on a live server
        without previews: identical 4.6 MB posters at 0 ms, 10 min and 50 min
        (phase 8 handoff), so byte equality is the honest test.
        """
        try:
            a = self._get(f"/library/metadata/{rating_key}/thumb/{int(ms_a)}",
                          accept="image/jpeg", raw=True)
            b = self._get(f"/library/metadata/{rating_key}/thumb/{int(ms_b)}",
                          accept="image/jpeg", raw=True)
        except PlexError:
            return False
        return bool(a) and bool(b) and a != b

    def frame_at(self, rating_key: str, ms: int, width: int = 720,
                 height: int = 405) -> tuple[bytes, str] | None:
        """A JPEG frame at ``ms`` via the photo transcoder, or None.

        The transcoder keeps the payload small (measured ~80 KB vs ~4.6 MB
        direct) at the cost of ~1 s. Falls back to the direct thumb when the
        transcoder refuses. Only call after :meth:`timeline_probe` passed.
        """
        thumb = f"/library/metadata/{rating_key}/thumb/{int(ms)}"
        quoted = urllib.parse.quote(thumb, safe="")
        paths = (
            f"/photo/:/transcode?url={quoted}&width={int(width)}&height={int(height)}"
            "&minSize=1&upscale=0",
            thumb,
        )
        for path in paths:
            try:
                data = self._get(path, accept="image/jpeg", raw=True)
            except PlexError:
                continue
            mime = _sniff_image_mime(data)
            if mime:
                return data, mime
        return None

    def poster(self, rating_key: str, width: int = 160,
               height: int = 240) -> tuple[bytes, str] | None:
        """Poster art for an item (show/season poster for episodes), or None.

        Proxied through the photo transcoder so multi-megabyte posters arrive
        as ~10 KB thumbnails and the Plex token never leaves the server.
        """
        try:
            md = self.metadata(rating_key)
        except PlexError:
            md = {}
        thumb = (md.get("grandparentThumb") or md.get("parentThumb")
                 or md.get("thumb") or f"/library/metadata/{rating_key}/thumb")
        quoted = urllib.parse.quote(thumb, safe="")
        paths = (
            f"/photo/:/transcode?url={quoted}&width={int(width)}&height={int(height)}"
            "&minSize=1&upscale=0",
            thumb,
        )
        for path in paths:
            try:
                data = self._get(path, accept="image/jpeg", raw=True)
            except PlexError:
                continue
            mime = _sniff_image_mime(data)
            if mime:
                return data, mime
        return None

    def recently_added(self, limit: int = 10) -> list[dict]:
        """The most recent items across all libraries, newest first."""
        payload = self._get("/library/recentlyAdded")
        items = payload.get("MediaContainer", {}).get("Metadata", []) or []
        return items[:limit]


def _sniff_image_mime(data: bytes | None) -> str | None:
    """Magic-byte check so a Plex error page or JSON blob is never a "frame"."""
    if not data or len(data) < 12:
        return None
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def classify_title(metadata: dict) -> dict:
    """What the build would do with this item, decided from one metadata response."""
    chapters = metadata.get("Chapter") or []
    streams = text_streams(metadata)
    sidecars = [s for s in streams if s.get("key")]
    embedded = [s for s in streams if not s.get("key")]
    if len(chapters) >= 4:
        segmentation = "chapters"
    elif streams:
        segmentation = "dialogue_gaps"
    else:
        segmentation = "fixed_windows"
    if sidecars:
        transcript = "sidecar"
    elif embedded:
        transcript = "embedded_only"
    else:
        transcript = "none"
    return {
        "title": metadata.get("title"),
        "year": metadata.get("year"),
        "guids": [g.get("id") for g in metadata.get("Guid") or []],
        "chapters": len(chapters),
        "segmentation": segmentation,
        "text_subtitle_streams": len(streams),
        "sidecar_subtitles": len(sidecars),
        "embedded_subtitles": len(embedded),
        "transcript": transcript,
        "timed_cues_possible": transcript != "none",
    }


def text_streams(metadata: dict) -> list[dict]:
    out = []
    for media in metadata.get("Media", []) or []:
        for part in media.get("Part", []) or []:
            for stream in part.get("Stream", []) or []:
                if stream.get("streamType") == 3 and (stream.get("codec") or "").lower() in TEXT_SUB_CODECS:
                    out.append(stream)
    return out


def classify_preparation(metadata: dict) -> dict:
    """Classify one item's subtitle readiness — the RESEARCH-PREPARATION.md §12
    failure taxonomy as a pure function over one metadata response.

    Product rule from the research: a library with no subtitles is a
    *successful detection*, never a plugin failure — so this function never
    raises and every state carries a user-facing ``message`` plus a ``remedy``
    naming the next action.

    States: ``sidecar`` (a fetchable subtitle file exists), ``embedded`` (text
    subs inside the video file, Plex cannot serve them), ``image_only``
    (picture-based subs; OCR unsupported), ``none`` (no subtitle track at
    all), ``unknown`` (metadata incomplete — retry the fetch).
    """
    if not isinstance(metadata, dict) or not metadata.get("Media"):
        return {"state": "unknown", **_PREP_STATE_DETAIL["unknown"]}

    streams = [
        stream
        for media in metadata.get("Media") or []
        for part in media.get("Part") or []
        for stream in part.get("Stream") or []
        if stream.get("streamType") == 3
    ]
    codecs = {(s.get("codec") or "").lower() for s in streams}
    if not streams:
        state = "none"
    elif codecs & TEXT_SUB_CODECS:
        text = [s for s in streams
                if (s.get("codec") or "").lower() in TEXT_SUB_CODECS]
        state = "sidecar" if any(s.get("key") for s in text) else "embedded"
    elif codecs & IMAGE_SUB_CODECS:
        state = "image_only"
    else:
        # Subtitle streams exist but the codec is unrecognised — treat as
        # embedded text rather than guessing "none".
        state = "embedded"
    return {"state": state, **_PREP_STATE_DETAIL[state]}


#: Per-state user-facing copy. ``remedy`` names the next action; ``None`` for
#: ``sidecar`` because the state itself is the good outcome (the next action,
#: fetching the sidecar, is the pipeline's normal job, not a fix).
_PREP_STATE_DETAIL = {
    "sidecar": {
        "message": "A subtitle file is available — timed notes can be built.",
        "remedy": "fetch the sidecar subtitle",
    },
    "embedded": {
        "message": ("Subtitle is inside the video file, so Plex can't serve it. "
                    "Download one in Plex (Search… on this title), or set an "
                    "extract command."),
        "remedy": "download subtitles in Plex, or set an extract command",
    },
    "image_only": {
        "message": ("This file has picture-based subtitles only. "
                    "Downloading a text subtitle in Plex fixes it."),
        "remedy": "download a text subtitle in Plex",
    },
    "none": {
        "message": ("No subtitles in this file, and none found for your "
                    "languages. This title can't produce timed notes."),
        "remedy": "download a subtitle in Plex",
    },
    "unknown": {
        "message": "Metadata was incomplete, so this item could not be classified.",
        "remedy": "retry the metadata fetch",
    },
}


def normalise_continue_watching_item(md: dict) -> dict:
    """One hub/flat Continue Watching item → the plugin's normalised shape.

    Field names follow the live response (RESEARCH-PREPARATION.md §9.1,
    verified against a captured hub). ``viewOffset`` is absent on the
    verified server — the queue is "next up" entries — so it defaults to 0.
    """
    return {
        "rating_key": str(md.get("ratingKey") or ""),
        "type": md.get("type"),
        "title": md.get("title"),
        "grandparent_title": md.get("grandparentTitle"),
        "view_offset_ms": int(md.get("viewOffset") or 0),
        "duration_ms": int(md.get("duration") or 0),
        "parent_index": md.get("parentIndex"),
        "index": md.get("index"),
        "parent_rating_key": str(md.get("parentRatingKey") or "") or None,
        "grandparent_rating_key": str(md.get("grandparentRatingKey") or "") or None,
        "key": md.get("key"),
        "parent_key": md.get("parentKey"),
        "grandparent_key": md.get("grandparentKey"),
    }


def normalise_session(md: dict) -> dict:
    player = md.get("Player", {}) or {}
    session = md.get("Session", {}) or {}
    user = md.get("User", {}) or {}
    selected = [
        s.get("id")
        for media in md.get("Media", []) or []
        for part in media.get("Part", []) or []
        for s in part.get("Stream", []) or []
        if s.get("streamType") == 3 and s.get("selected")
    ]
    return {
        "session_id": str(session.get("id") or md.get("sessionKey") or md.get("ratingKey")),
        "rating_key": str(md.get("ratingKey")),
        "media_type": md.get("type"),
        "title": md.get("title"),
        "grandparent_title": md.get("grandparentTitle"),
        "season": md.get("parentIndex"),
        "episode": md.get("index"),
        "duration_ms": int(md.get("duration") or 0),
        "offset_ms": int(md.get("viewOffset") or 0),
        "state": (player.get("state") or "unknown").lower(),
        "player_title": player.get("title"),
        "player_machine_identifier": player.get("machineIdentifier"),
        "user_title": user.get("title"),
        "location": session.get("location"),
        "transcode_key": (md.get("TranscodeSession") or {}).get("key"),
        "selected_subtitle_id": selected[0] if selected else None,
    }
