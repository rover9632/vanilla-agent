"""Injectable time source (docs/plan.md T0.3).

Every wall-clock or monotonic read in the service goes through the *current*
:class:`Clock`, so tests can freeze and advance virtual time instead of
sleeping for real. Production code never calls ``datetime.now`` /
``time.monotonic`` directly for behavior that tests must pin (interrupt
timeouts T8.1, idle TTLs T6.4, LRU bookkeeping, token-window buckets T13.4).

Semantics:
- ``now()`` / ``now_ms()`` are wall time (UTC-aware / unix epoch ms).
  ``now_ms()`` is what :mod:`app.core.ids` stamps into UUIDv7 (T0.3).
- ``monotonic()`` is a never-going-backward seconds counter with an
  unspecified origin — measure durations with it, never compare it to dates.
- ``sleep()`` is the awaited suspension point. With :class:`FakeClock` it
  advances virtual time and returns immediately (yielding once to the event
  loop); with :class:`SystemClock` it is ``asyncio.sleep``.

Known limit of virtual time: anything that schedules on the real event loop
(``asyncio.wait_for``, ``loop.call_later``, uvicorn timeouts) is unaffected
by ``FakeClock.advance``. Code that must be testable against a timeout
therefore polls ``monotonic()`` itself or is driven explicitly in tests.

The current clock is process-global (ADR-014: one process, one loop) —
swap it with :func:`use_clock`, which restores the previous value on exit
and nests correctly. ``pytest`` fixtures should prefer
:meth:`FakeClock.install` for scoped replacement.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

# Default virtual epoch for FakeClock: a fixed date so tests that do not
# pass a start still get deterministic, readable timestamps.
DEFAULT_FAKE_START = datetime(2026, 1, 1, tzinfo=UTC)


@runtime_checkable
class Clock(Protocol):
    """The time surface the rest of the app is allowed to see."""

    def now(self) -> datetime:
        """Current wall time as a timezone-aware UTC datetime."""
        ...

    def now_ms(self) -> int:
        """Current wall time as whole milliseconds since the unix epoch."""
        ...

    def monotonic(self) -> float:
        """Seconds from an unspecified origin; never decreases."""
        ...

    async def sleep(self, delay: float) -> None:
        """Suspend for *delay* seconds (virtual seconds under FakeClock)."""
        ...


class SystemClock:
    """Real wall + monotonic time. The default clock of the process."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_ms(self) -> int:
        # time_ns avoids the float round-trip of time.time() * 1000.
        return time.time_ns() // 1_000_000

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


class FakeClock:
    """Virtual clock: time moves only when a test says so.

    ``advance()`` moves wall and monotonic time together, so durations
    measured across sleeps are exact. ``sleep()`` consumes virtual time and
    returns immediately — a loop that sleeps 100 times is instant — but it
    yields to the event loop once so concurrent tasks still interleave.

    Virtual time never runs backward (negative ``advance`` is a programming
    error, not a scenario worth supporting).
    """

    def __init__(self, start: datetime = DEFAULT_FAKE_START) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock start must be a timezone-aware datetime")
        self._wall = start.timestamp()
        self._mono = 0.0

    # -- test controls ---------------------------------------------------------

    def advance(self, seconds: float) -> None:
        """Move virtual time forward by *seconds* (wall and monotonic)."""
        if seconds < 0:
            raise ValueError(f"cannot move virtual time backwards ({seconds})")
        self._wall += seconds
        self._mono += seconds

    def install(self) -> AbstractContextManager[Clock]:
        """Make this clock the current one until the returned context exits.

        ``with FakeClock().install(): ...``
        """
        return use_clock(self)

    # -- Clock protocol ----------------------------------------------------------

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._wall, tz=UTC)

    def now_ms(self) -> int:
        # int() floors; sub-ms drift of a float epoch is irrelevant here, and
        # flooring keeps now_ms <= the true virtual instant (never "future").
        return int(self._wall * 1000)

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, delay: float) -> None:
        if delay < 0:
            raise ValueError(f"cannot sleep for a negative duration ({delay})")
        self.advance(delay)
        await asyncio.sleep(0)


# --- current clock (process-global; see module docstring) --------------------

_current: Clock = SystemClock()


def get_clock() -> Clock:
    """The clock consumers use right now."""
    return _current


def set_clock(clock: Clock) -> Clock:
    """Replace the current clock; returns the previous one.

    Prefer :func:`use_clock` / :meth:`FakeClock.install` — this bare setter
    exists for the context manager and for one-way process startup overrides.
    """
    global _current
    previous = _current
    _current = clock
    return previous


@contextmanager
def use_clock(clock: Clock) -> Generator[Clock]:
    """Scope the current clock to *clock*, restoring on exit (nests)."""
    previous = set_clock(clock)
    try:
        yield clock
    finally:
        set_clock(previous)


# --- module-level convenience (reads the *current* clock per call) -----------


def now() -> datetime:
    return _current.now()


def now_ms() -> int:
    return _current.now_ms()


def monotonic() -> float:
    return _current.monotonic()


async def sleep(delay: float) -> None:
    await _current.sleep(delay)
