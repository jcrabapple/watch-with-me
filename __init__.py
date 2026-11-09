"""Hermes entry point for watch-with-me. Everything lives in the package."""

from __future__ import annotations

if __package__:  # loaded by Hermes as a plugin package
    from .watch_with_me.plugin import register  # noqa: F401
