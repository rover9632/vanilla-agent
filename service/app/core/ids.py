"""Monotonic, sortable entity identifiers — UUIDv7 (docs/plan.md T0.3).

Thread / run / message / interrupt / artifact ids are UUIDs. ``uuid4`` would
be fine as identity but useless for ordering: paging a table by
``created_at`` needs clock precision, and re-sorting in memory breaks once
rows share a timestamp. ``uuid1`` orders but leaks the host (MAC-derived
node id), which the publishable-repo rules and §8 care about. UUIDv7
(RFC 9562) is the middle path:

    unix_ts_ms (48) | version=0b0111 (4) | seq (12) | variant=0b10 (2) | rand (62)

- **Sorts by creation time** by construction (lexicographic over the raw
  128-bit value, which ``UUID.__lt__`` uses).
- **Process-monotonic**: ids minted inside the same millisecond get an
  incrementing 12-bit ``seq`` instead of fresh randomness; once 4096 ids fit
  in one ms, ``seq`` overflows and the embedded ms is advanced by one. A
  backward clock jump (NTP) is absorbed the same way — the high-water mark
  never goes back, so ids this process already handed out are never
  re-issued or re-ordered.
- **No host fingerprint**: the trailing 62 bits are OS randomness; the seq
  start is randomized per millisecond so two processes on one host do not
  produce adjacent id streams.

The timestamp comes from :mod:`app.core.clock` (T0.3), so tests drive id
time with ``FakeClock``. ``str(uuid)`` is the canonical 36-char form the DB
stores (TEXT) and the API returns (JSON); ``.hex`` is the compact form used
for request ids.
"""

from __future__ import annotations

import os
import threading
from uuid import UUID

from app.core import clock

_VERSION = 0b0111  # bits 76-79: UUIDv7
_VARIANT = 0b10  # bits 62-63: RFC 4122 variant
_TS_BITS = 48
_SEQ_BITS = 12
_SEQ_MAX = (1 << _SEQ_BITS) - 1  # 4095
_RAND_BITS = 62
_TS_MASK = (1 << _TS_BITS) - 1
_RAND_MASK = (1 << _RAND_BITS) - 1

# High-water state. A threading.Lock guards it (cheap) because sync threadpool
# endpoints could otherwise interleave; within the asyncio loop it never
# contends. Tests reset it via _reset_state_for_tests().
_lock = threading.Lock()
_state_ms = 0
_state_seq = 0


def _mint_int(now_ms: int) -> int:
    global _state_ms, _state_seq
    with _lock:
        ms = now_ms & _TS_MASK
        if ms > _state_ms:
            # New millisecond: start the seq at a random point so id streams
            # from different processes are not adjacent/observable.
            _state_seq = int.from_bytes(os.urandom(2), "big") & _SEQ_MAX
        else:
            # Clock stands still or jumped back: stay on the high-water mark
            # and keep incrementing. On seq exhaustion, one ms forward.
            ms = _state_ms
            if _state_seq >= _SEQ_MAX:
                ms = _state_ms + 1
                _state_seq = 0
            else:
                _state_seq += 1
        _state_ms = ms
        rand = int.from_bytes(os.urandom(8), "big") & _RAND_MASK
        return (
            (ms << 80) | (_VERSION << 76) | (_state_seq << 64) | (_VARIANT << 62) | rand
        )


def new_id() -> UUID:
    """Mint a fresh UUIDv7. Monotonic within this process, time-sortable
    across processes."""
    return UUID(int=_mint_int(clock.now_ms()))


def id_timestamp_ms(id: UUID) -> int:
    """The embedded unix-ms of a UUIDv7 (approximate creation time; may run
    ahead of the wall clock by up to one ms per 4096 ids minted in the same
    ms, and after a backward clock correction)."""
    return id.int >> 80


def _reset_state_for_tests() -> None:
    """Forget the high-water mark (test isolation only — never call from
    application code)."""
    global _state_ms, _state_seq
    with _lock:
        _state_ms = 0
        _state_seq = 0
