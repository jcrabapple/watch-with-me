"""Resolve which model writes the notes — one resolver, two callers.

Precedence (highest first):

1. The plugin setting ``note_model`` / ``note_provider`` (explicit override),
   honoured only when the host allows it
   (``plugins.entries.watch-with-me.llm.allow_model_override: true``).
2. ``auxiliary.watch_with_me_cues`` in the Hermes config — the auxiliary-model
   picker entry created by ``ctx.register_auxiliary_task``.
3. The ``inherit_from="compression"`` task's effective config
   (``auxiliary.compression``).
4. Standalone CLI only: the ``WWM_MODEL`` environment variable, then
   ``OPENROUTER_MODEL``.

When nothing resolves, :func:`require_model` fails with a message naming the
two ways to set it. Inside Hermes the environment is never consulted: the host
routes auxiliary calls itself, so an env-sourced name would describe a model
the gateway would not actually use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional

AUX_TASK = "watch_with_me_cues"
AUX_ASK_TASK = "watch_with_me_ask"
INHERIT_TASK = "compression"
PLUGIN_ID = "watch-with-me"

ENV_VARS = ("WWM_MODEL", "OPENROUTER_MODEL")

_OVERRIDE_HINT = ("set 'plugins.entries.watch-with-me.llm.allow_model_override: true' "
                  "to honour the note_model override")

_GUIDANCE_OVERRIDE = ("Using your explicit note_model override; per-film spend follows that model.")
_GUIDANCE_PICKER = ("A flash-tier model is sufficient for notes; a reasoning model costs several "
                    "times more and can return empty content when the token budget goes to "
                    "reasoning tokens.")
_GUIDANCE_INHERITED = ("No picker entry yet; inheriting the compression model. Choose a flash model "
                       "under auxiliary.watch_with_me_cues to control note cost.")
_GUIDANCE_ENV = ("Using a model from the environment for this standalone build.")
_GUIDANCE_NONE = ("Not configured — set auxiliary.watch_with_me_cues in the Hermes config, or "
                  "export WWM_MODEL (or OPENROUTER_MODEL) for standalone builds.")


class ModelResolutionError(RuntimeError):
    """No model could be resolved for the note build."""


@dataclass
class ModelChoice:
    model: str          # resolved model id, "" when nothing is configured
    source: str         # override, picker, inherited, env, or none
    override_allowed: bool
    override_requested: bool = False  # note_model is set but was not honoured


def model_unset_message() -> str:
    return ("no note model configured: set 'auxiliary.watch_with_me_cues' in the Hermes "
            "config (the auxiliary-model picker), or export WWM_MODEL / OPENROUTER_MODEL "
            "for a standalone build")


def load_hermes_config() -> dict:
    """Best-effort read of the host's config.yaml.

    In the gateway process the host's own loader wins (it honours HERMES_HOME
    and any config overlays). Standalone, parse ``$HERMES_HOME/config.yaml``
    (default ``~/.hermes/config.yaml``) directly. Any failure degrades to an
    empty dict, which the resolver treats as "nothing configured".
    """
    try:  # gateway process: authoritative, overlay-aware
        from hermes_cli.config import load_config_readonly  # type: ignore[import-not-found]

        config = load_config_readonly()
        if isinstance(config, dict):
            return config
    except Exception:
        pass
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    path = home / "config.yaml"
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore[import-not-found]

        data = yaml.safe_load(path.read_text("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def model_override_allowed(config: Mapping[str, Any]) -> bool:
    """Whether the host honours plugin provider/model overrides for this plugin."""
    try:
        llm = config["plugins"]["entries"][PLUGIN_ID]["llm"]
        return isinstance(llm, Mapping) and llm.get("allow_model_override") is True
    except (KeyError, TypeError):
        return False


def plugin_settings(config: Mapping[str, Any]) -> dict:
    """This plugin's settings subtree from the host config (tolerant of absence)."""
    try:
        entry = config["plugins"]["entries"][PLUGIN_ID]
        if isinstance(entry, Mapping):
            settings = entry.get("settings")
            if isinstance(settings, Mapping):
                return dict(settings)
    except (KeyError, TypeError):
        pass
    return {}


def live_plugin_settings() -> dict:
    """This plugin's saved settings from the LIVE host config.

    Read on every watcher poll / API call so an edit under Hermes
    Settings → Plugins lands without a gateway restart. Standalone (no host
    config readable) this is ``{}`` and the plugin defaults apply.
    """
    try:
        return plugin_settings(load_hermes_config())
    except Exception:  # noqa: BLE001 - a config read glitch degrades to defaults
        return {}


def gateway_injection_allowed(config: Mapping[str, Any]) -> bool:
    """Whether the host will deliver ``ctx.inject_message`` for this plugin.

    The flag is the plugin-entry key ``allow_gateway_injection: true`` in
    config.yaml (default false in the host), NOT a plugin setting — the
    plugin cannot grant itself the capability, it can only surface the state.
    """
    try:
        entry = config["plugins"]["entries"][PLUGIN_ID]
        return isinstance(entry, Mapping) and entry.get("allow_gateway_injection") is True
    except (KeyError, TypeError):
        return False


def _aux_model(entry: Any) -> str:
    """The model from an ``auxiliary.<task>`` config entry (dict or plain string)."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, Mapping):
        model = entry.get("model")
        return model.strip() if isinstance(model, str) else ""
    return ""


def resolve_model(settings, *, config: Optional[Mapping[str, Any]] = None,
                  env: Optional[Mapping[str, str]] = None,
                  standalone: bool = False) -> ModelChoice:
    """Resolve the note model per the documented precedence."""
    if config is None:
        config = load_hermes_config()
    if env is None:
        # Read only the two variables this module consults; handing the whole
        # process environment to a function trips the plugin security scanner.
        env = {name: os.environ[name] for name in ENV_VARS if name in os.environ}
    allowed = model_override_allowed(config)
    requested = bool(getattr(settings, "note_model", ""))

    if requested and allowed:
        return ModelChoice(settings.note_model, "override", True, override_requested=True)

    aux = _aux_model((config.get("auxiliary") or {}).get(AUX_TASK))
    if aux:
        return ModelChoice(aux, "picker", allowed, override_requested=requested)

    inherited = _aux_model((config.get("auxiliary") or {}).get(INHERIT_TASK))
    if inherited:
        return ModelChoice(inherited, "inherited", allowed, override_requested=requested)

    if standalone:
        for var in ENV_VARS:
            value = env.get(var)
            if value:
                return ModelChoice(value, "env", allowed, override_requested=requested)

    return ModelChoice("", "none", allowed, override_requested=requested)


def require_model(settings, *, config: Optional[Mapping[str, Any]] = None,
                  env: Optional[Mapping[str, str]] = None,
                  standalone: bool = False) -> str:
    """Resolve or fail with a message naming both ways to configure the model."""
    choice = resolve_model(settings, config=config, env=env, standalone=standalone)
    if not choice.model:
        raise ModelResolutionError(model_unset_message())
    return choice.model


def guidance(choice: ModelChoice) -> str:
    if choice.override_requested and not choice.override_allowed:
        return (f"note_model is set but the host has not allowed model overrides — {_OVERRIDE_HINT}.")
    return {
        "override": _GUIDANCE_OVERRIDE,
        "picker": _GUIDANCE_PICKER,
        "inherited": _GUIDANCE_INHERITED,
        "env": _GUIDANCE_ENV,
        "none": _GUIDANCE_NONE,
    }.get(choice.source, _GUIDANCE_NONE)


def status_payload(*, config: Optional[Mapping[str, Any]] = None,
                   env: Optional[Mapping[str, str]] = None) -> dict:
    """What the dashboard and `/watch status` show about model selection."""
    from .config import Settings

    if config is None:
        config = load_hermes_config()
    settings = Settings.from_dict(plugin_settings(config))
    choice = resolve_model(settings, config=config, env=env)
    return {
        "model": choice.model,
        "model_source": choice.source,
        "model_override_allowed": choice.override_allowed,
        "model_override_requested": choice.override_requested,
        "note_effort": settings.note_effort,
        "entity_expansion": settings.entity_expansion,
        "note_density_seconds": settings.note_density_seconds,
        "guidance": guidance(choice),
    }


def gateway_completer(ctx, settings, *, config: Optional[Mapping[str, Any]] = None):
    """The in-gateway note completer: routes through the ``watch_with_me_cues``
    auxiliary task, applying the resolver's outcome.

    An honoured override is passed explicitly (the host gates ``model=`` on
    ``allow_model_override``); every other source routes purely by task key, so
    the gateway and this resolver agree by construction. Returns
    ``(complete, choice)``.
    """
    choice = resolve_model(settings, config=config)
    kwargs: MutableMapping[str, Any] = {"task": AUX_TASK}
    if choice.source == "override":
        kwargs["model"] = choice.model
        if getattr(settings, "note_provider", ""):
            kwargs["provider"] = settings.note_provider

    def complete(prompt: str) -> str:
        result = ctx.llm.complete([{"role": "user", "content": prompt}], **kwargs)
        return (getattr(result, "text", "") or "").strip()

    return complete, choice
