"""Pure filtering policy: which Plex sessions may become a watch session."""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_TYPES = {"movie", "episode"}


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str


def _player_allowed(session: dict, allowed_players: list[str]) -> bool:
    """Match a machine id, a player title, or the id with Plex's client suffix.

    Plex reports `player_machine_identifier` as `<machineIdentifier>-<client>`,
    as in `aa8eae38106ba773-com-plexapp-android`, so comparing the raw value to
    the bare machine id from /identity never matches.
    """
    ident = session.get("player_machine_identifier") or ""
    title = session.get("player_title") or ""
    base = ident.split("-", 1)[0]
    for allowed in allowed_players:
        if allowed in (ident, base, title):
            return True
        if ident.startswith(f"{allowed}-"):
            return True
    return False


def evaluate(session: dict, settings, *, known_view_count: int=0, seen_at: float | None = None,
             now: float | None = None) -> Decision:
    """Decide whether a normalised Plex session may become a candidate."""
    if session.get("media_type") not in SUPPORTED_TYPES:
        return Decision(False, "unsupported_media_type")

    # An empty allowlist must not mean "anyone": on a shared server that is the
    # difference between a personal feed and other people's viewing habits.
    if not settings.allowed_user_ids:
        return Decision(False, "user_allowlist_unconfigured")
    if (session.get("user_title") or "") not in settings.allowed_user_ids:
        return Decision(False, "user_not_allowed")

    allowed_players = settings.allowed_player_ids or []
    if allowed_players and not _player_allowed(session, allowed_players):
        return Decision(False, "player_not_allowed")

    if session.get("location") not in (None, "lan"):
        # Remote streams are allowed by default; kept explicit so the rule is visible.
        pass

    if not settings.rewatch and known_view_count > 0:
        return Decision(False, "already_watched")

    if seen_at is not None and now is not None and now - seen_at < 30 * 86400:
        return Decision(False, "dismissed_recently")

    return Decision(True, "eligible")


def should_promote(state: str, playing_seconds: float, settings) -> bool:
    """auto promotes after the dwell; ask/manual never promote themselves."""
    if settings.session_start != "auto":
        return False
    return state == "playing" and playing_seconds >= settings.candidate_seconds
