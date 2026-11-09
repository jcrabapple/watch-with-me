#!/usr/bin/env python3
"""Phase 0 Plex probes. Read-only; writes sanitized artifacts only.

    python scripts/probe_plex.py cadence --seconds 180 --client web --out artifacts/phase0/cadence-web.jsonl
    python scripts/probe_plex.py metadata --rating-key 1234 --out artifacts/phase0/metadata.json
    python scripts/probe_plex.py sidecar --stream-key /library/streams/5678 --ext srt --out artifacts/phase0/sub.srt
    python scripts/probe_plex.py save-credentials

The token is read from PLEX_TOKEN, or prompted without echo. It is never printed
and never written into an artifact. `save-credentials` is the one place it is
persisted, with mode 0600, for the watcher to use.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watch_with_me.plex import PlexClient, PlexError, classify_title, text_streams  # noqa: E402
from watch_with_me import onboarding  # noqa: E402


def _client(args) -> PlexClient:
    token = os.environ.get("PLEX_TOKEN") or getpass.getpass("Plex token: ")
    if not args.base_url:
        raise SystemExit("--base-url is required (for example http://192.168.1.20:32400)")
    return PlexClient(args.base_url, token, args.client_id,
                      allow_loopback=args.allow_loopback)


def _gaps(offsets: list[tuple[float, int]]) -> dict:
    """Characterise Plex's reporting cadence.

    `offsets` is (sample_time, viewOffset) in order. A "change event" is the
    sample where the offset differs from the previous one; the interval we care
    about is the time BETWEEN change events, not the time between the sample
    before and after one (that is just the polling interval).
    """
    events: list[tuple[float, int]] = []
    prev: tuple[float, int] | None = None
    for sample in offsets:
        if prev is not None and sample[1] != prev[1]:
            events.append((sample[0], sample[1] - prev[1]))
        prev = sample
    if len(events) < 2:
        return {"samples": len(offsets), "change_events": len(events)}
    intervals = sorted(b[0] - a[0] for a, b in zip(events, events[1:]))
    jumps = sorted(abs(e[1]) for e in events)
    return {
        "samples": len(offsets),
        "change_events": len(events),
        "update_interval_median_s": round(statistics.median(intervals), 2),
        "update_interval_p95_s": round(intervals[min(len(intervals) - 1, int(len(intervals) * 0.95))], 2),
        "update_interval_max_s": round(intervals[-1], 2),
        "update_interval_min_s": round(intervals[0], 2),
        "median_jump_ms": jumps[len(jumps) // 2],
        "stalls_over_30s": sum(1 for i in intervals if i > 30),
    }


def cmd_cadence(args) -> int:
    print(f"resolving and polling {args.base_url} as {args.client} for {args.seconds}s "
          f"(progress every {args.progress_every} samples)", flush=True)
    client = _client(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    offsets: list[tuple[float, int]] = []
    samples = 0
    empty_polls = 0
    deadline = time.monotonic() + args.seconds
    started_at = time.monotonic()
    file = out.open("w", encoding="utf-8")
    try:
        while time.monotonic() < deadline:
            started = time.time()
            try:
                sessions = client.sessions()
            except PlexError as exc:
                print(f"poll failed: {exc}", flush=True)
                time.sleep(args.interval)
                continue
            if not sessions:
                empty_polls += 1
                if empty_polls in (5, 30):
                    print(f"no active sessions after {empty_polls} polls; is playback running "
                          f"and is this the right server?", flush=True)
            for session in sessions:
                file.write(json.dumps({"at": round(started, 3), "client": args.client,
                                       **session}) + "\n")
                offsets.append((started, session["offset_ms"]))
            samples += len(sessions)
            if samples and samples % args.progress_every == 0:
                last = offsets[-1]
                print(f"{samples} samples, offset {last[1] // 60000}:{last[1] // 1000 % 60:02d}, "
                      f"{session['state']}", flush=True)
            file.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("interrupted; summarising what was collected", flush=True)
    finally:
        file.close()
    summary = _gaps(offsets)
    summary["client"] = args.client
    summary["windows_seconds"] = round(time.monotonic() - started_at, 1)
    summary["empty_polls"] = empty_polls
    report = out.with_suffix(".summary.json")
    report.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    print(f"wrote {out} and {report}")
    return 0


def cmd_metadata(args) -> int:
    client = _client(args)
    metadata = client.metadata(args.rating_key)
    streams = text_streams(metadata)
    sanitized = {
        "ratingKey": metadata.get("ratingKey"),
        "type": metadata.get("type"),
        "title": metadata.get("title"),
        "year": metadata.get("year"),
        "duration": metadata.get("duration"),
        "guid": metadata.get("guid"),
        "Guid": metadata.get("Guid"),
        "chapterSource": metadata.get("chapterSource"),
        "Chapter": [
            {"index": c.get("index"), "startTimeOffset": c.get("startTimeOffset"),
             "endTimeOffset": c.get("endTimeOffset")}
            for c in metadata.get("Chapter", []) or []
        ],
        "audio": [
            {"codec": s.get("codec"), "languageCode": s.get("languageCode"),
             "selected": s.get("selected")}
            for m in metadata.get("Media", []) or []
            for p in m.get("Part", []) or []
            for s in p.get("Stream", []) or [] if s.get("streamType") == 2
        ],
        "text_subtitles": [
            {"id": s.get("id"), "codec": s.get("codec"), "languageCode": s.get("languageCode"),
             "key": s.get("key"), "location": s.get("location"), "selected": s.get("selected"),
             "title": s.get("extendedDisplayTitle")}
            for s in streams
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")
    print(json.dumps(sanitized, indent=2))
    print(f"wrote {out}")
    return 0


def cmd_diagnose(args) -> int:
    client = _client(args)
    metadata = client.metadata(args.rating_key)
    result = classify_title(metadata)
    print(json.dumps(result, indent=2))
    if result["transcript"] == "sidecar":
        print("-> full pipeline: transcript cues with anchors")
    elif result["transcript"] == "embedded_only":
        print("-> no sidecar transcript: configure OpenSubtitles or extract_command, "
              "or let Plex download subtitles so a sidecar exists. Otherwise metadata-only.")
    else:
        print("-> no text subtitles at all: metadata card and digest only")
    if args.fetch_sidecar:
        sidecars = [s for s in text_streams(metadata) if s.get("key")]
        if not sidecars:
            print("-> no sidecar stream to fetch (all text streams are embedded)")
            return 0
        stream = sidecars[0]
        key = stream["key"]
        try:
            body = client.sidecar(key, stream.get("codec") or "srt")
        except PlexError as exc:
            print(f"sidecar fetch failed: {exc}", flush=True)
            return 2
        cues = body.count("-->")
        first = next((line for line in body.splitlines() if "-->" in line), "")
        print(json.dumps({"sidecar_key": key, "bytes": len(body), "cue_lines": cues,
                          "first_cue": first}, indent=2))
    return 0


def cmd_transcode_sub(args) -> int:
    """Try every documented spelling of the transcode subtitle route.

    Plex's reference names the parameter `transcodeSessionId`; community clients
    use `session` with either the session key or its bare UUID. Try all of them
    and report which, if any, returns a body.
    """
    client = _client(args)
    target = next((s for s in client.sessions() if s["rating_key"] == args.rating_key), None)
    if target is None:
        print("no active session for that rating key", flush=True)
        return 1
    print(json.dumps({k: target.get(k) for k in (
        "session_id", "state", "transcode_key", "selected_subtitle_id", "player_title")}, indent=2))
    key = target.get("transcode_key")
    if not key:
        print("-> no transcode session: the client is direct-playing, so this route has nothing to serve")
        return 1
    if not target.get("selected_subtitle_id"):
        print("-> no subtitle selected in the session; turn subtitles on in the client and retry")
        return 1

    uuid = key.rsplit("/", 1)[-1]
    base = (f"/video/:/transcode/universal/subtitles?path=%2Flibrary%2Fmetadata%2F"
            f"{args.rating_key}&encoding=utf-8&X-Plex-Platform=Chrome")
    sid = target["selected_subtitle_id"]
    variants = [
        ("session-uuid", f"{base}&session={uuid}"),
        ("session-key", f"{base}&session={key}"),
        ("transcodeSessionId-uuid", f"{base}&transcodeSessionId={uuid}"),
        ("transcodeSessionId-key", f"{base}&transcodeSessionId={key}"),
        ("session-uuid-embedded", f"{base}&session={uuid}&subtitles=embedded"),
        ("session-uuid-streamid", f"{base}&session={uuid}&subtitlesStreamID={sid}"),
        ("session-uuid-embedded-streamid",
         f"{base}&session={uuid}&subtitles=embedded&subtitlesStreamID={sid}"),
    ]
    if args.variant:
        variants = [v for v in variants if v[0] == args.variant] or variants

    for name, path in variants:
        body = _read_bounded(client, path, args.read_seconds)
        text = body.decode("utf-8", "replace")
        print(f"{name:<34} bytes={len(body)} cues={text.count('-->')}", flush=True)
        if body:
            first = next((l for l in text.splitlines() if "-->" in l), "")
            print(json.dumps({"winning_variant": name, "bytes": len(body),
                              "cue_lines": text.count("-->"), "first_line": first}, indent=2))
            return 0
        if name == "session-uuid":
            print("  (accepted but nothing arrived in the read window)")
    print("-> no variant returned a body")
    return 2


def _read_bounded(client, path: str, seconds: int) -> bytes:
    """Read a streaming response for a bounded window, keeping whatever arrives.

    Plex paces this stream and never closes it, so read() would block forever.
    """
    import http.client
    import urllib.request

    url = f"{client.base_url}{path}"
    req = urllib.request.Request(url, headers={
        "X-Plex-Token": client.token,
        "X-Plex-Client-Identifier": client.client_id,
        "Accept": "text/plain",
    })
    buf = bytearray()
    deadline = time.time() + seconds
    try:
        with urllib.request.urlopen(req, timeout=seconds + 2) as resp:
            while time.time() < deadline:
                try:
                    chunk = resp.read(8192)
                except http.client.IncompleteRead as exc:
                    buf += exc.partial or b""
                    break
                except OSError:
                    # socket.timeout and Python 3.14's "cannot read from timed out
                    # object" both land here; keep whatever already arrived.
                    break
                if not chunk:
                    break
                buf += chunk
    except urllib.error.HTTPError as exc:
        raise PlexError(f"Plex HTTP {exc.code}", status=exc.code) from exc
    except OSError:
        pass
    return bytes(buf)


def cmd_sidecar(args) -> int:
    client = _client(args)
    try:
        body = client.sidecar(args.stream_key, args.ext)
    except PlexError as exc:
        print(f"sidecar failed: {exc} (501 means not a sidecar)", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(json.dumps({"ok": True, "bytes": len(body), "lines": body.count("\n"), "out": str(out)}))
    return 0


def cmd_discovery(args) -> int:
    """plex.tv server discovery. Writes names and connection URIs only."""
    token = os.environ.get("PLEX_TOKEN") or getpass.getpass("Plex token: ")
    payload = onboarding.fetch_resources(token, args.client_id)
    servers = onboarding.filter_server_resources(payload)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(servers, indent=2), encoding="utf-8")
    print(json.dumps({"servers": len(servers), "out": str(out)}))
    return 0


def cmd_save_credentials(args) -> int:
    """Development path: store a manually obtained token via the shared writer."""
    token = os.environ.get("PLEX_TOKEN") or getpass.getpass("Plex token: ")
    if not args.base_url:
        raise SystemExit("--base-url is required")
    path = onboarding.save_credentials(
        base_url=args.base_url, token=token, client_id=args.client_id,
        server_name=args.server_name or "", machine_id=args.machine_id or "")
    print(json.dumps({"ok": True, "path": str(path),
                      "mode": oct(path.stat().st_mode & 0o777)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("PLEX_BASE_URL", ""))
    parser.add_argument("--client-id", default="watch-with-me-phase0")
    parser.add_argument("--allow-loopback", action="store_true")
    # Subcommands accept the same flags so `cmd --base-url ...` works too.
    # SUPPRESS keeps an absent subcommand flag from overwriting the global one.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default=argparse.SUPPRESS)
    common.add_argument("--client-id", default=argparse.SUPPRESS)
    common.add_argument("--allow-loopback", action="store_true", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("cadence", parents=[common])
    p.add_argument("--seconds", type=int, default=180)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--progress-every", type=int, default=10)
    p.add_argument("--client", default="unknown")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_cadence)

    p = sub.add_parser("metadata", parents=[common])
    p.add_argument("--rating-key", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_metadata)

    p = sub.add_parser("diagnose", parents=[common])
    p.add_argument("--rating-key", required=True)
    p.add_argument("--fetch-sidecar", action="store_true",
                   help="also fetch the first sidecar subtitle and report cue lines")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("transcode-sub", parents=[common])
    p.add_argument("--rating-key", required=True)
    p.add_argument("--read-seconds", type=int, default=20,
                   help="how long to read the stream before reporting (default 20)")
    p.add_argument("--variant", default="", help="try only one named variant")
    p.set_defaults(func=cmd_transcode_sub)

    p = sub.add_parser("sidecar", parents=[common])
    p.add_argument("--stream-key", required=True)
    p.add_argument("--ext", default="srt")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_sidecar)

    p = sub.add_parser("discovery", parents=[common])
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_discovery)

    p = sub.add_parser("save-credentials", parents=[common])
    p.add_argument("--server-name", default="", help="recorded for display after linking")
    p.add_argument("--machine-id", default="", help="the server's clientIdentifier")
    p.set_defaults(func=cmd_save_credentials)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
