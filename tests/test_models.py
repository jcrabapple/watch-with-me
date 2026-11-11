from __future__ import annotations

import json
import types

import pytest

from watch_with_me import build, models
from watch_with_me.config import Settings


def cfg(*, aux=None, inherit=None, override=None):
    """Minimal Hermes config fragment for resolver tests."""
    config: dict = {}
    if aux is not None or inherit is not None:
        config["auxiliary"] = {}
        if aux is not None:
            config["auxiliary"]["watch_with_me_cues"] = aux
        if inherit is not None:
            config["auxiliary"]["compression"] = inherit
    if override is not None:
        config["plugins"] = {"entries": {"watch-with-me": {"llm": {"allow_model_override": override}}}}
    return config


class TestConfigSchemaDefaults:
    def test_plugin_yaml_defaults_match_settings_defaults(self):
        """Every config_schema default must equal the Settings() default.

        The host renders config_schema defaults in Settings → Plugins and
        merges saved values over them; a mismatch means the UI shows one
        thing while the watcher applies another."""
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parent.parent
        schema = yaml.safe_load(
            (root / "plugin.yaml").read_text("utf-8"))["config_schema"]
        defaults = Settings()
        assert schema, "plugin.yaml carries no config_schema"
        for key, spec in schema.items():
            assert hasattr(defaults, key), \
                f"config_schema key {key!r} does not exist on Settings"
            assert "default" in spec, f"config_schema key {key!r} has no default"
            assert spec["default"] == getattr(defaults, key), (
                f"config_schema default for {key!r} ({spec['default']!r}) != "
                f"Settings default ({getattr(defaults, key)!r})")


class TestSettingsFields:
    def test_new_fields_have_documented_defaults(self):
        settings = Settings()
        assert settings.note_model == ""
        assert settings.note_provider == ""
        assert settings.note_effort == "low"
        assert settings.entity_expansion is True
        assert settings.note_density_seconds == 180

    def test_from_dict_parses_new_fields(self):
        settings = Settings.from_dict({"note_model": "z-ai/glm-5.3-flash", "note_provider": "openrouter",
                                       "note_effort": "medium", "entity_expansion": False,
                                       "note_density_seconds": 90})
        assert settings.note_model == "z-ai/glm-5.3-flash"
        assert settings.note_provider == "openrouter"
        assert settings.note_effort == "medium"
        assert settings.entity_expansion is False
        assert settings.note_density_seconds == 90

    def test_from_dict_tolerates_unknown_keys(self):
        settings = Settings.from_dict({"bogus_future_key": 1})
        assert settings.note_model == ""
        assert settings.entity_expansion is True


class TestModelOverrideFlag:
    def test_override_allowed_reads_plugin_entry(self):
        assert models.model_override_allowed(cfg(override=True)) is True

    def test_override_flag_absent_is_false(self):
        assert models.model_override_allowed(cfg()) is False
        assert models.model_override_allowed({}) is False

    def test_override_flag_false_is_false(self):
        assert models.model_override_allowed(cfg(override=False)) is False


class TestResolveModel:
    def test_override_wins_when_allowed(self):
        settings = Settings(note_model="custom/model")
        choice = models.resolve_model(settings, config=cfg(aux={"model": "picker/model"}, override=True))
        assert choice.model == "custom/model"
        assert choice.source == "override"
        assert choice.override_allowed is True

    def test_override_skipped_when_flag_false_and_picker_used(self):
        settings = Settings(note_model="custom/model")
        choice = models.resolve_model(settings, config=cfg(aux={"model": "picker/model"}, override=False))
        assert choice.model == "picker/model"
        assert choice.source == "picker"
        assert choice.override_allowed is False
        assert choice.override_requested is True  # UI must show the override is NOT in effect

    def test_override_skipped_when_flag_absent(self):
        settings = Settings(note_model="custom/model")
        choice = models.resolve_model(settings, config=cfg(aux={"model": "picker/model"}))
        assert choice.source == "picker"
        assert choice.override_requested is True

    def test_override_alone_without_any_config_is_unset(self):
        settings = Settings(note_model="custom/model")
        choice = models.resolve_model(settings, config=cfg())
        assert choice.model == ""
        assert choice.source == "none"
        assert choice.override_requested is True

    def test_picker_entry_as_dict(self):
        choice = models.resolve_model(Settings(), config=cfg(aux={"model": "glm", "provider": "openrouter"}))
        assert choice.model == "glm"
        assert choice.source == "picker"

    def test_picker_entry_as_plain_string(self):
        choice = models.resolve_model(Settings(), config=cfg(aux="glm"))
        assert choice.model == "glm"
        assert choice.source == "picker"

    def test_inherits_compression_when_picker_entry_absent(self):
        choice = models.resolve_model(Settings(), config=cfg(inherit={"model": "deepseek-flash"}))
        assert choice.model == "deepseek-flash"
        assert choice.source == "inherited"

    def test_picker_beats_inherit(self):
        choice = models.resolve_model(Settings(),
                                      config=cfg(aux={"model": "glm"}, inherit={"model": "deepseek-flash"}))
        assert choice.source == "picker"

    def test_env_fallback_used_in_standalone_mode(self):
        choice = models.resolve_model(Settings(), config={}, env={"WWM_MODEL": "env/model"},
                                      standalone=True)
        assert choice.model == "env/model"
        assert choice.source == "env"

    def test_openrouter_model_is_the_second_env_fallback(self):
        choice = models.resolve_model(Settings(), config={},
                                      env={"OPENROUTER_MODEL": "or/model"}, standalone=True)
        assert choice.model == "or/model"
        assert choice.source == "env"

    def test_wwm_model_beats_openrouter_model(self):
        choice = models.resolve_model(Settings(), config={},
                                      env={"WWM_MODEL": "a", "OPENROUTER_MODEL": "b"}, standalone=True)
        assert choice.model == "a"

    def test_env_not_used_inside_hermes(self):
        # In-gateway the host resolves routing itself; env must not claim a model
        # the auxiliary client will not actually use.
        choice = models.resolve_model(Settings(), config={}, env={"WWM_MODEL": "env/model"})
        assert choice.source == "none"
        assert choice.model == ""


class TestRequireModel:
    def test_hard_failure_message_names_both_ways_to_set_it(self):
        with pytest.raises(models.ModelResolutionError) as excinfo:
            models.require_model(Settings(), config={}, env={}, standalone=True)
        message = str(excinfo.value)
        assert "auxiliary.watch_with_me_cues" in message
        assert "WWM_MODEL" in message

    def test_require_model_returns_resolved_model(self):
        assert models.require_model(Settings(), config=cfg(aux={"model": "glm"}), env={}) == "glm"


class TestStatusPayload:
    def test_payload_exposes_override_allowed_and_source(self):
        payload = models.status_payload(config=cfg(aux={"model": "glm"}), env={})
        assert payload["model"] == "glm"
        assert payload["model_source"] == "picker"
        assert payload["model_override_allowed"] is False
        assert payload["model_override_requested"] is False
        assert payload["guidance"]

    def test_payload_carries_the_existing_settings_fields(self):
        config = cfg(aux={"model": "glm"})
        config["plugins"] = {"entries": {"watch-with-me": {
            "llm": {"allow_model_override": True},
            "settings": {"note_effort": "medium", "entity_expansion": False,
                         "note_density_seconds": 90, "note_model": "custom/model"},
        }}}
        payload = models.status_payload(config=config, env={})
        assert payload["model"] == "custom/model"
        assert payload["model_source"] == "override"
        assert payload["model_override_allowed"] is True
        assert payload["note_effort"] == "medium"
        assert payload["entity_expansion"] is False
        assert payload["note_density_seconds"] == 90

    def test_guidance_covers_the_three_dashboard_states(self):
        override = models.status_payload(config=cfg(aux={"model": "g"}, override=True), env={})
        override["model"] = "custom"  # source stays picker here; craft states directly instead
        picker = models.status_payload(config=cfg(aux={"model": "g"}), env={})
        none = models.status_payload(config=cfg(), env={})
        assert picker["guidance"]
        assert none["guidance"]
        assert "Not configured" in json.dumps(none)


class FakeLlm:
    def __init__(self):
        self.calls: list[tuple] = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return types.SimpleNamespace(text="{\"notes\": []}")


class FakeCtx:
    def __init__(self):
        self.llm = FakeLlm()


class TestGatewayCompleter:
    def test_routes_through_the_aux_task(self):
        ctx = FakeCtx()
        complete, choice = models.gateway_completer(ctx, Settings(), config=cfg(aux={"model": "g"}))
        assert choice.source == "picker"
        complete("prompt")
        _, kwargs = ctx.llm.calls[0]
        assert kwargs["task"] == "watch_with_me_cues"
        assert "model" not in kwargs

    def test_allowed_override_passes_model_and_provider(self):
        ctx = FakeCtx()
        settings = Settings(note_model="custom/model", note_provider="openrouter")
        complete, choice = models.gateway_completer(ctx, settings, config=cfg(override=True))
        assert choice.source == "override"
        complete("prompt")
        _, kwargs = ctx.llm.calls[0]
        assert kwargs["model"] == "custom/model"
        assert kwargs["provider"] == "openrouter"
        assert kwargs["task"] == "watch_with_me_cues"

    def test_refused_override_does_not_pass_model(self):
        # The host would raise a trust error; the completer must not claim an
        # override the host will refuse.
        ctx = FakeCtx()
        settings = Settings(note_model="custom/model")
        complete, choice = models.gateway_completer(ctx, settings, config=cfg(aux={"model": "g"}))
        assert choice.source == "picker"
        complete("prompt")
        _, kwargs = ctx.llm.calls[0]
        assert "model" not in kwargs

    def test_returns_text(self):
        ctx = FakeCtx()
        complete, _ = models.gateway_completer(ctx, Settings(), config=cfg(aux={"model": "g"}))
        assert complete("prompt") == '{"notes": []}'


class TestEntityExpansionOffLeavesBuildUnchanged:
    def test_no_person_facts_matches_none(self):
        metadata = {"ratingKey": "1", "type": "movie", "title": "T", "year": 2000,
                    "duration": 7_200_000, "Chapter": [],
                    "Role": [{"tag": "Someone", "role": "a role"}]}
        srt = ("1\n00:00:10,000 --> 00:00:12,000\nHe was a good friend.\n\n"
               "2\n00:01:00,000 --> 00:01:02,000\nThe shield generator is down.\n")

        def complete(prompt: str) -> str:
            return json.dumps({"messages": [{"kind": "cast", "text": "A note.",
                                             "fact_ids": ["f1"], "anchor": "He was a good friend.",
                                             "confidence": 0.9}]})

        baseline = build.build_messages(metadata, srt, [], complete)
        without_expansion = build.build_messages(metadata, srt, [], complete, person_facts=[])
        assert without_expansion.messages == baseline.messages
        assert without_expansion.state == baseline.state == "ready"


class TestPluginStatus:
    def test_status_payload_includes_model_fields(self, tmp_path, monkeypatch):
        from watch_with_me import plugin, store
        conn = store.connect(tmp_path / "status.db")
        monkeypatch.setattr(plugin, "_connection", lambda: conn)
        monkeypatch.setattr(models, "load_hermes_config",
                            lambda: cfg(aux={"model": "g"}, override=True))
        body = json.loads(plugin._status())
        assert body["model"] == "g"
        assert body["model_source"] == "picker"
        assert body["model_override_allowed"] is True
        assert body["model_override_requested"] is False
        assert body["guidance"]
