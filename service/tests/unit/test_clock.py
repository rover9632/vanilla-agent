"""Unit tests for app.core.clock (docs/plan.md T0.3)."""

from __future__ import annotations

import itertools
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock as clock_mod
from app.core.clock import (
    DEFAULT_FAKE_START,
    Clock,
    FakeClock,
    SystemClock,
    get_clock,
    set_clock,
    use_clock,
)


@pytest.fixture(autouse=True)
def _restore_current_clock():
    """No test may leak a swapped clock into the next one."""
    previous = get_clock()
    yield
    set_clock(previous)


# --- SystemClock ------------------------------------------------------------------


def test_system_clock_reads_real_time() -> None:
    c = SystemClock()
    now = c.now()
    assert now.utcoffset() == timedelta(0)
    assert abs(now.timestamp() - time.time()) < 5
    assert abs(c.now_ms() - time.time_ns() // 1_000_000) < 5000


def test_system_clock_monotonic_never_goes_back() -> None:
    c = SystemClock()
    samples = [c.monotonic() for _ in range(100)]
    assert all(b >= a for a, b in itertools.pairwise(samples))


async def test_system_clock_sleeps_for_real() -> None:
    c = SystemClock()
    started = c.monotonic()
    await c.sleep(0.02)
    # 20 ms target with loop-granularity slack: the selector loop computes
    # its deadline on loop.time(), which can resolve a hair before
    # time.monotonic() crosses it — an exact-boundary assert would be flaky.
    assert c.monotonic() - started >= 0.015


def test_clocks_satisfy_the_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FakeClock(), Clock)


# --- FakeClock basics ----------------------------------------------------------


def test_fake_clock_starts_at_the_fixed_epoch() -> None:
    c = FakeClock()
    assert c.now() == DEFAULT_FAKE_START
    assert c.now_ms() == int(DEFAULT_FAKE_START.timestamp() * 1000)
    assert c.monotonic() == 0.0


def test_fake_clock_start_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 1, 1))  # naive: silently local-zone, rejected


def test_advance_moves_wall_and_monotonic_together() -> None:
    c = FakeClock()
    c.advance(90.5)
    assert c.now() == datetime.fromtimestamp(
        DEFAULT_FAKE_START.timestamp() + 90.5, tz=UTC
    )
    assert c.monotonic() == 90.5
    assert c.now_ms() == int((DEFAULT_FAKE_START.timestamp() + 90.5) * 1000)


def test_advance_rejects_negative() -> None:
    c = FakeClock()
    with pytest.raises(ValueError, match="backwards"):
        c.advance(-0.001)


def test_now_ms_floors_and_stays_consistent_with_now() -> None:
    c = FakeClock()
    c.advance(1.2349)
    assert c.now_ms() == int(DEFAULT_FAKE_START.timestamp() * 1000) + 1234
    assert c.now_ms() <= c.now().timestamp() * 1000


async def test_sleep_consumes_virtual_time_not_wall_time() -> None:
    c = FakeClock()
    real_start = time.monotonic()
    for _ in range(100):  # 100 virtual seconds must not cost real seconds
        await c.sleep(1.0)
    assert c.monotonic() == 100.0
    assert time.monotonic() - real_start < 1.0


async def test_sleep_rejects_negative() -> None:
    c = FakeClock()
    with pytest.raises(ValueError, match="negative"):
        await c.sleep(-1)


# --- current-clock plumbing --------------------------------------------------------


def test_set_clock_returns_the_previous_one() -> None:
    original = get_clock()
    fake = FakeClock()
    assert set_clock(fake) is original
    assert get_clock() is fake
    assert set_clock(original) is fake


def test_use_clock_restores_on_exception() -> None:
    original = get_clock()
    fake = FakeClock()
    with pytest.raises(RuntimeError), use_clock(fake):
        raise RuntimeError("boom")
    assert get_clock() is original


def test_use_clock_nests() -> None:
    outer, inner = FakeClock(), FakeClock()
    with use_clock(outer):
        with use_clock(inner):
            assert get_clock() is inner
        assert get_clock() is outer
    assert isinstance(get_clock(), SystemClock)


def test_fake_clock_install_is_a_scoped_swap() -> None:
    fake = FakeClock()
    with fake.install():
        assert get_clock() is fake
    assert isinstance(get_clock(), SystemClock)


async def test_module_level_helpers_delegate_to_the_current_clock() -> None:
    fake = FakeClock()
    fake.advance(10)
    with use_clock(fake):
        assert clock_mod.now() == fake.now()
        assert clock_mod.now_ms() == fake.now_ms()
        assert clock_mod.monotonic() == 10.0
        await clock_mod.sleep(5)
        assert clock_mod.monotonic() == 15.0
