from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_title  # noqa: E402
from watch_with_me import models  # noqa: E402
from watch_with_me.config import Settings  # noqa: E402


def test_episode_metadata_queries_the_series_name():
    metadata = {"type": "episode", "title": "A Bad Lie", "grandparentTitle": "Elsbeth",
                "year": 2025}
    assert build_title.wikipedia_title(metadata) == "Elsbeth"


def test_episode_without_a_series_falls_back_to_the_episode_title():
    metadata = {"type": "episode", "title": "Ozymandias"}
    assert build_title.wikipedia_title(metadata) == "Ozymandias"


def test_film_metadata_queries_its_own_title():
    metadata = {"type": "movie", "title": "The Brutalist", "year": 2025}
    assert build_title.wikipedia_title(metadata) == "The Brutalist"


class TestFetchArticleExtract:
    def test_episode_queries_tv_series_form_without_the_episode_year(self):
        calls = []

        def fetch(title, year):
            calls.append((title, year))
            return "series article text" if title == "9-1-1 (TV series)" else ""

        metadata = {"type": "episode", "title": "That Sinking Feeling",
                    "grandparentTitle": "9-1-1", "year": 2026}
        extract, query_title, query_year = build_title.fetch_article_extract(metadata,
                                                                             fetch=fetch)
        assert extract == "series article text"
        assert query_title == "9-1-1 (TV series)"
        assert query_year is None
        # The year must never be passed for an episode: fetch_extract appends
        # "(year film)", which misses every series article.
        assert calls == [("9-1-1 (TV series)", None)]

    def test_episode_falls_back_to_the_plain_series_name(self):
        def fetch(title, year):
            return "plain series article" if title == "Elsbeth" else ""

        metadata = {"type": "episode", "title": "A Bad Lie",
                    "grandparentTitle": "Elsbeth", "year": 2025}
        extract, query_title, query_year = build_title.fetch_article_extract(metadata,
                                                                             fetch=fetch)
        assert extract == "plain series article"
        assert query_title == "Elsbeth"
        assert query_year is None

    def test_movie_queries_title_with_year_unchanged(self):
        calls = []

        def fetch(title, year):
            calls.append((title, year))
            return "film article"

        metadata = {"type": "movie", "title": "The Brutalist", "year": 2025}
        extract, query_title, query_year = build_title.fetch_article_extract(metadata,
                                                                             fetch=fetch)
        assert extract == "film article"
        assert (query_title, query_year) == ("The Brutalist", 2025)
        assert calls == [("The Brutalist", 2025)]


class TestPickModel:
    def test_no_hardcoded_default_model_anywhere(self):
        import argparse
        parser = argparse.ArgumentParser()
        build_title.add_model_arguments(parser)
        defaults = [action.default for action in parser._actions if action.dest == "model"]
        assert defaults == [""]

    def test_cli_model_argument_wins(self):
        model, choice = build_title.pick_model(Settings(), "cli/model", env={})
        assert model == "cli/model"

    def test_env_fallback_resolves_in_standalone_mode(self):
        model, choice = build_title.pick_model(Settings(), "", env={"WWM_MODEL": "env/model"},
                                               config={})
        assert model == "env/model"
        assert choice.source == "env"

    def test_unset_model_fails_naming_both_ways_to_set_it(self):
        with pytest.raises(SystemExit) as excinfo:
            build_title.pick_model(Settings(), "", env={}, config={})
        message = str(excinfo.value)
        assert "auxiliary.watch_with_me_cues" in message
        assert "WWM_MODEL" in message

    def test_plugin_settings_are_read_from_hermes_config(self):
        config = {"auxiliary": {"watch_with_me_cues": {"model": "picker/model"}},
                  "plugins": {"entries": {"watch-with-me": {
                      "llm": {"allow_model_override": True},
                      "settings": {"note_model": "override/model"}}}}}
        settings = build_title.load_settings(config)
        model, choice = build_title.pick_model(settings, "", env={}, config=config)
        assert model == "override/model"
        assert choice.source == "override"

    def test_refused_override_warns_and_falls_back(self, capsys):
        config = {"auxiliary": {"watch_with_me_cues": {"model": "picker/model"}},
                  "plugins": {"entries": {"watch-with-me": {
                      "settings": {"note_model": "override/model"}}}}}
        settings = build_title.load_settings(config)
        model, choice = build_title.pick_model(settings, "", env={}, config=config)
        assert model == "picker/model"
        assert "allow_model_override" in capsys.readouterr().err


class TestEntityExpansion:
    def test_disabled_never_fetches_cast_facts(self):
        def boom(_name):
            raise AssertionError("fetch must not be called when entity expansion is off")

        assert build_title.fetch_person_facts({}, False, boom) == []

    def test_enabled_fetches_cast_facts(self, monkeypatch):
        monkeypatch.setattr(build_title.cast, "actor_facts",
                            lambda metadata, fetch: ["fact one"])
        assert build_title.fetch_person_facts({}, True, lambda name: "x") == ["fact one"]


class TestReasoningDefault:
    def test_reasoning_falls_back_to_settings_note_effort(self):
        assert build_title.effective_reasoning("", Settings()) == "low"
        assert build_title.effective_reasoning("", Settings(note_effort="medium")) == "medium"
        assert build_title.effective_reasoning("high", Settings()) == "high"
