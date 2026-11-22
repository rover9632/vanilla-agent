"""Unit tests for app.core.ids — UUIDv7 monotonicity and layout (docs/plan.md T0.3).

The plan's verification gate for T0.3 calls out id monotonicity explicitly;
these tests pin it under adversarial conditions: a frozen millisecond (the
seq path), seq overflow past 4096 ids/ms (the forced-ms-advance path), and
a backward clock jump (NTP), all driven through ``FakeClock`` so timing is
exact rather than measured.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.core import ids
from app.core.clock import DEFAULT_FAKE_START, FakeClock, use_clock


@pytest.fixture(autouse=True)
def _fresh_id_state():
    """The high-water mark is module-global; tests must not inherit it.

    Without the reset, a test running on a FakeClock whose ms is *earlier*
    than state left behind by a previous (system-clock or later-fake) test
    would land on the increment path and its embedded ts would not match
    the fake clock — exactly the thing some tests below assert.
    """
    ids._reset_state_for_tests()
    yield
    ids._reset_state_for_tests()


# --- structure ------------------------------------------------------------------


def test_uuid7_version_and_variant_bits() -> None:
    u = ids.new_id()
    assert u.version == 7
    assert (u.int >> 62) & 0b11 == 0b10  # RFC 4122 variant


def test_canonical_string_round_trip() -> None:
    u = ids.new_id()
    assert UUID(str(u)) == u
    assert len(str(u)) == 36


def test_embedded_timestamp_is_the_clock_ms() -> None:
    fake = FakeClock()
    with use_clock(fake):
        u = ids.new_id()
    assert ids.id_timestamp_ms(u) == int(DEFAULT_FAKE_START.timestamp() * 1000)


# --- monotonicity (the T0.3 gate) ------------------------------------------------


def test_strictly_increasing_within_one_millisecond() -> None:
    """Frozen clock: every id must still be strictly greater than the last.

    5000 ids exceed the 12-bit seq space (4096), so this exercises the
    overflow branch (ms advanced by one) as well as plain increments.
    """
    with use_clock(FakeClock()):
        minted = [ids.new_id() for _ in range(5000)]
    assert all(b > a for a, b in itertools.pairwise(minted))
    assert len(set(minted)) == len(minted)


def test_time_sortable_across_clock_advances() -> None:
    fake = FakeClock()
    minted = []
    with use_clock(fake):
        for _ in range(50):
            minted.append(ids.new_id())
            fake.advance(0.037)  # 37 ms steps
    assert minted == sorted(minted)


def test_backward_clock_jump_is_absorbed() -> None:
    """NTP correction must never yield an id <= one already handed out."""
    with use_clock(FakeClock(datetime(2026, 6, 1, tzinfo=UTC))):
        before = ids.new_id()
    with use_clock(FakeClock(datetime(2025, 1, 1, tzinfo=UTC))):
        after = ids.new_id()
    assert after > before


def test_same_ms_ids_differ_in_random_tail() -> None:
    with use_clock(FakeClock()):
        a, b = ids.new_id(), ids.new_id()
    assert (a.int & ((1 << 62) - 1)) != (b.int & ((1 << 62) - 1))


# --- integration with the HTTP layer ----------------------------------------------


def test_request_ids_are_ordered_by_mint_time() -> None:
    """new_request_id() (core/logging) rides the same generator, so log/id
    timelines sort naturally."""
    from app.core.logging import new_request_id

    with use_clock(FakeClock()):
        a, b = new_request_id(), new_request_id()
    assert a < b  # hex strings of increasing ints, equal length
