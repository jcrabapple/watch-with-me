"""Continue Watching prewarm ordering — the ordered list of items to prepare.

Shape only: this module decides *which* items a preparation pass would
process, and in what order. It performs no scheduling, no budgets, no
downloads and no library mutations (RESEARCH-PREPARATION.md §9, §12).

Depth vocabulary (``Settings.prepare_depth``):

- ``off``   → nothing.
- ``next``  → the Continue Watching items themselves, in hub order. Each CW
              entry is already the next-unwatched item of its show
              (§9.2: ``viewOffset`` is 0 on every entry), so "next" is the
              queue exactly as Plex reports it.
- ``two``   → each CW item plus the one following episode of the same show
              (2 per show where one exists). The following episode is found
              in the show's ``allLeaves`` sorted by ``(parentIndex, index)``,
              which crosses season boundaries correctly (§9.3).
- ``queue`` → every item in the Continue Watching hub.

``next`` and ``queue`` currently return the same list — both are the whole
hub. The distinction is the promise, not the paging: ``queue`` means "all of
the queue, however long", while ``next`` means "one preparation per queued
show". They diverge only if a future depth ever expands or truncates.
"""

from __future__ import annotations

from collections.abc import Callable

from .plex import PlexClient

DEPTH_VALUES = ("off", "next", "two", "queue")


def prewarm_items(
    items: list[dict],
    depth: str,
    leaves_lookup: Callable[[str], list[dict]] | None = None,
) -> list[dict]:
    """The ordered, de-duplicated preparation list for one Continue Watching
    snapshot. Pure: network access arrives only through ``leaves_lookup``.

    ``items`` are the normalised hub rows from
    ``PlexClient.continue_watching()`` (see ``normalise_continue_watching_item``).
    ``leaves_lookup`` maps a show ``rating_key`` to its ``allLeaves`` episodes
    in air order; it is consulted only for depth ``two``. Without it, ``two``
    degrades to the unexpanded queue rather than failing.
    """
    if depth == "off":
        return []
    queue = _dedupe(items)
    if depth in ("next", "queue"):
        return queue
    if depth == "two":
        if leaves_lookup is None:
            return queue
        out: list[dict] = []
        show_cache: dict[str, list[dict]] = {}
        for item in queue:
            out.append(item)
            show_key = item.get("grandparent_rating_key")
            if not show_key or item.get("type") != "episode":
                continue
            if show_key not in show_cache:
                show_cache[show_key] = leaves_lookup(show_key)
            following = _following_episode(item, show_cache[show_key])
            if following is not None:
                out.append(following)
        return _dedupe(out)
    raise ValueError(f"unknown prepare_depth {depth!r} (expected one of {DEPTH_VALUES})")


def collect_prewarm_queue(client: PlexClient, depth: str) -> list[dict]:
    """The live prewarm list for the configured depth.

    Depth ``off`` never touches the network. Depth ``two`` makes one
    ``allLeaves`` call per distinct show in the queue (~0.10 s each, §9.3).
    """
    if depth == "off":
        return []
    items = client.continue_watching()
    if depth != "two":
        return prewarm_items(items, depth)
    return prewarm_items(items, depth, leaves_lookup=client.show_leaves)


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = str(item.get("rating_key") or item.get("ratingKey") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(item)
    return out


def _following_episode(item: dict, leaves: list[dict]) -> dict | None:
    """The episode after ``item`` in air order, or None at the end of the show.

    ``leaves`` is the show's ``allLeaves`` sorted by ``(parentIndex, index)``.
    Matching is by ``ratingKey``; a CW item missing from the leaves (stale
    queue) simply yields no expansion.
    """
    rating_key = str(item.get("rating_key") or "")
    for position, leaf in enumerate(leaves):
        if str(leaf.get("ratingKey") or "") == rating_key:
            if position + 1 < len(leaves):
                return _normalise_leaf(leaves[position + 1])
            return None
    return None


def _normalise_leaf(md: dict) -> dict:
    """An ``allLeaves`` row → the same shape as a hub item, so the preparation
    list is homogeneous regardless of which endpoint produced the entry."""
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
