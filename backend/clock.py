"""One clock.

A pool has ONE real absolute deadline in epoch seconds. `clock_scale` is display-only:
it compresses the countdown the UI renders and divides cooldowns so a 90-second demo
feels like a three-day pool. The agent is always given real seconds — the unit it also
sleeps in — so guards only ever clamp, never convert. Dual-clock arithmetic inside a
guard is a silent demo-killer that only surfaces on camera; this module exists so there
is exactly one place it could live, and it doesn't.
"""
from __future__ import annotations

import time


def now() -> float:
    return time.time()


def time_left_s(deadline_at: float | None) -> int:
    """Real seconds until the deadline, floored at 0."""
    if not deadline_at:
        return 0
    return max(0, int(deadline_at - time.time()))


def reached(deadline_at: float | None, grace_s: float = 0.0) -> bool:
    if not deadline_at:
        return False
    return time.time() >= (deadline_at + grace_s)


def human_label(seconds: int) -> str:
    """'2d 14h', '3h 05m', '45s' — what the agent quotes to a human."""
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


def display_left_s(real_left_s: int, clock_scale: int) -> int:
    """What the on-screen countdown shows: real seconds stretched by the scale."""
    return max(0, int(real_left_s) * max(1, int(clock_scale)))


def display_label(real_left_s: int, clock_scale: int) -> str:
    return human_label(display_left_s(real_left_s, clock_scale))


def scaled_cooldown(real_cooldown_s: float, clock_scale: int) -> float:
    """A demo pool compresses time, so cooldowns compress with it."""
    return real_cooldown_s / max(1, int(clock_scale))
