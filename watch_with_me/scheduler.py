"""Pure clock arithmetic for the coarse-anchor scheduler.

No I/O, no threads. `viewOffset` is a client-reported snapshot with 5-30 s
granularity, so this model treats it as an anchor and predicts forward.
"""

from __future__ import annotations


def predicted_offset_ms(base_offset_ms: int, base_at_monotonic: float, now_monotonic: float,
                        state: str) -> int:
    """Where playback should be, given the last anchor. Frozen unless playing."""
    if state != "playing":
        return int(base_offset_ms)
    elapsed = max(0.0, now_monotonic - base_at_monotonic)
    return int(base_offset_ms + elapsed * 1000)


def is_discontinuity(predicted_ms: int, reported_ms: int, threshold_ms: int = 15000) -> bool:
    """True when the report cannot be explained by reporting lag alone."""
    return abs(int(reported_ms) - int(predicted_ms)) > max(1000, int(threshold_ms))


def due_messages(messages: list[dict], offset_ms: int, delivered_ids: set[str]) -> list[dict]:
    """Messages whose timestamp has been reached and which were not delivered.

    Returns at most one, the earliest, so a seek cannot emit a burst.
    """
    candidates = [
        m for m in messages
        if int(m["t_ms"]) <= int(offset_ms) and m["id"] not in delivered_ids
    ]
    candidates.sort(key=lambda m: int(m["t_ms"]))
    return candidates[:1]


def skipped_by_seek(messages: list[dict], predicted_ms: int, reported_ms: int,
                    delivered_ids: set[str]) -> list[dict]:
    """Messages crossed by a forward seek, suppressed instead of fired late."""
    if reported_ms <= predicted_ms:
        return []
    return [
        m for m in messages
        if predicted_ms < int(m["t_ms"]) <= reported_ms and m["id"] not in delivered_ids
    ]


def classify_sample(predicted_ms: int, reported_ms: int, threshold_ms: int = 15000) -> str:
    """Label a playing snapshot as a normal update or a seek.

    Both rebase the clock: the reported offset is ground truth. The label only
    decides whether already-due messages should be suppressed, which is what a
    forward seek requires. Measured Plex Web cadence is ~10.2 s, so with a 20 s
    threshold a skipped update is still an update; a real skip is a seek.
    """
    return "seek" if abs(int(reported_ms) - int(predicted_ms)) > max(1000, int(threshold_ms)) else "update"
