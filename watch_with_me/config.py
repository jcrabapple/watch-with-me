"""Plugin-owned settings with defaults.

Phase 0 keeps this deliberately small: the names are the contract, the storage
mechanism is ctx.get_config/set_config once that is verified against the host.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Settings:
    # Session policy
    session_start: str = "auto"          # auto | ask | manual
    candidate_seconds: int = 20
    dismiss_after_seconds: int = 300
    rewatch: bool = False
    allowed_user_ids: list[str] = field(default_factory=list)
    allowed_player_ids: list[str] = field(default_factory=list)
    allowed_library_ids: list[str] = field(default_factory=list)

    # Sync
    poll_active_seconds: float = 3.0
    poll_idle_seconds: float = 20.0
    # 2x the measured Plex Web p95 cadence (10.3 s); a normal heartbeat lag
    # must not classify as a seek.
    rebase_threshold_ms: int = 20600
    poll_failures_before_disconnected: int = 3

    # Preparation
    prepare_depth: str = "off"           # off | next | two | queue
    prepare_budget_titles_per_day: int = 3
    prepare_budget_usd_per_day: float = 1.0
    # Maximum builds started in one idle slice (one sync cycle). The daily
    # budget above is the real spend cap; this only bounds a single run.
    prepare_per_run_cap: int = 3

    # Subtitle download (Plex search-and-attach; RESEARCH-PREPARATION.md §4, §14
    # Step 3). Off by default: attaching writes a subtitle file into the Plex
    # library, requires Plex Pass, and Plex may later remove it.
    download_subtitles: bool = False
    subtitle_languages: list[str] = field(default_factory=lambda: ["en"])
    subtitle_download_budget: int = 5

    # Transcript
    subtitle_language: str = "audio"
    extract_command: str = ""

    # Delivery
    chat_notifications: bool = False
    public_base_url: str = ""

    # Connection
    allow_loopback_plex: bool = False

    # Notes
    note_model: str = ""                 # explicit override; needs host allow_model_override
    note_provider: str = ""              # override provider; same gate as note_model
    note_effort: str = "low"             # reasoning effort for the note model
    entity_expansion: bool = True        # enrich notes with per-entity lookups
    note_density_seconds: int = 180      # target spacing between notes

    # Optional entity selection (Jev — a decision model chooses which named
    # entities are worth expanding, BEFORE the Wikipedia fetch). Off by
    # default; with it off or unreachable the build is byte-identical to no
    # Jev at all. jev_api_key is write-only: set it via the dashboard API
    # (stored mode 0600 in the plugin data dir) or the TYPESAFE_API_KEY /
    # OPENROUTER_API_KEY env vars — it never belongs in config.yaml.
    jev_enabled: bool = False
    jev_provider: str = "auto"           # auto | typesafe | openrouter
    jev_api_key: str = ""                # write-only; data dir / env, never config.yaml
    jev_model: str = ""                  # empty = provider default (typesafe/jev-1.13)

    # Ask the Projectionist (one-tap "what did I just see?")
    projectionist: bool = True           # off: /ask returns 404 and the button is not rendered

    # Note display (the Pause Reel). live: notes render as they are delivered.
    # pause: notes are still delivered and ledgered exactly as in live mode,
    # but the page holds them and reveals the accumulated group when playback
    # pauses (or when the waiting-counter is tapped). both: live rendering,
    # and the counter stays hidden. Default is "live" so competition demos
    # show the feed working; "pause" is the calmer default a normal viewer
    # should pick — the facts do not improve by arriving mid-dialogue, they
    # improve when there is a natural moment to look.
    note_display: str = "live"           # live | pause | both

    def as_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Settings":
        raw = raw or {}
        known = {f: raw[f] for f in cls.__dataclass_fields__ if f in raw}
        return cls(**known)


#: The consent text for ``download_subtitles``. Wherever the setting is
#: presented (docs, dashboard, chat), this is the wording — it states the four
#: facts the user is opting into: a library write, the Plex Pass requirement,
#: Plex's right to remove the subtitle later, and the plugin's own cached copy.
DOWNLOAD_SUBTITLES_HELP = (
    "Download subtitles with Plex's own subtitle search and attach them to the "
    "item: this WRITES subtitle files into your Plex library (visible to every "
    "user of the server), requires Plex Pass, and Plex may later remove them "
    "(for example when the selected subtitle track changes). The plugin "
    "immediately keeps its own cached copy, which is what the notes are built "
    "from afterwards."
)
