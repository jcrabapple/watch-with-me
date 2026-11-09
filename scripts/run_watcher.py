#!/usr/bin/env python3
"""Run the watcher standalone, against the real plugin data directory.

This is the demo rehearsal path: it exercises credentials, polling, the claim,
and delivery without enabling the plugin inside the Hermes gateway.

    python scripts/run_watcher.py
    python scripts/run_watcher.py --candidate-seconds 5 --poll 0.5
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watch_with_me import store  # noqa: E402
from watch_with_me.config import Settings  # noqa: E402
from watch_with_me.watcher import Watcher, credentials_path  # noqa: E402


def _env_list(name: str) -> list[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-seconds", type=int, default=20)
    parser.add_argument("--poll", type=float, default=0.0,
                        help="override the active poll interval (seconds)")
    parser.add_argument("--chat-notifications", action="store_true")
    parser.add_argument("--extract-command", default="")
    parser.add_argument("--user", action="append", default=[],
                        help="Plex user title allowed to claim (repeatable); "
                             "omit to allow any user")
    parser.add_argument("--player", action="append", default=[],
                        help="player machineIdentifier allowed to claim (repeatable)")
    args = parser.parse_args()

    path = credentials_path()
    if not path.exists():
        print(f"no credentials at {path}\n"
              f"run: scripts/probe_plex.py save-credentials --base-url http://<server>:32400",
              file=sys.stderr)
        return 2

    settings = Settings(candidate_seconds=args.candidate_seconds,
                        chat_notifications=args.chat_notifications,
                        extract_command=args.extract_command or os.environ.get("WWM_EXTRACT_COMMAND", "").strip(),
                        allowed_user_ids=args.user or _env_list("WWM_USER"),
                        allowed_player_ids=args.player or _env_list("WWM_PLAYER"))
    if args.poll:
        settings.poll_active_seconds = args.poll

    if not settings.allowed_user_ids:
        print("warning: no --user given, so no Plex user is eligible and nothing "
              "can be claimed. Pass --user <your Plex user title> (repeatable) to "
              "allow your streams, e.g. --user jason.",
              file=sys.stderr, flush=True)
    if not settings.allowed_player_ids:
        print("warning: no --player given, so any device on that account can claim. "
              "Pass --player <machineIdentifier> to pin one.",
              file=sys.stderr, flush=True)

    conn = store.connect()
    print(f"watching {path}  candidate={args.candidate_seconds}s  "
          f"poll={settings.poll_active_seconds}s", flush=True)

    watcher = Watcher(settings)
    if not watcher.start():
        print("another watcher already holds the lock", file=sys.stderr)
        return 1

    last = None
    try:
        while True:
            time.sleep(1.0)
            state = store.get_state(conn, "watcher_state", "?")
            claim = store.get_state(conn, "claimed_session", "")
            session = store.latest_session(conn)
            line = (state, claim, (session or {}).get("rating_key"),
                    (session or {}).get("state"))
            if line != last:
                delivered = len(store.delivered_ids(conn, claim)) if claim else 0
                print(f"state={state} claim={claim or '-'} "
                      f"item={(session or {}).get('title') or (session or {}).get('rating_key')} "
                      f"delivered={delivered}", flush=True)
                last = line
    except KeyboardInterrupt:
        print("stopping", flush=True)
    finally:
        watcher.stop()
        watcher.join(timeout=5)
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
