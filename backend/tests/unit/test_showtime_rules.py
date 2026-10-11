"""Unit tests for the pure showtime scheduling arithmetic (SPEC-3 Section 5).

Contains SPEC-3 tests 1, 2 and 3, plus a check that the instant-taking helpers
reject naive datetimes. No database and no HTTP are involved: every function
under test is called directly with plain values.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.showtimes.rules import conflict_window, day_bounds_utc, windows_overlap

# The default slot from SPEC-3 (SHOWTIME_SLOT_MINUTES = 180).
_SLOT_MINUTES = 180

# An arbitrary but fixed, timezone-aware instant so the arithmetic is
# deterministic and independent of the machine clock.
_BASE = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def test_windows_overlap_boundaries() -> None:
    """Test 1: gap < slot conflicts, gap == slot does not, symmetric."""
    # One minute short of the slot conflicts, on either side.
    assert (
        windows_overlap(
            _BASE, _BASE + timedelta(minutes=_SLOT_MINUTES - 1), _SLOT_MINUTES
        )
        is True
    )
    assert (
        windows_overlap(
            _BASE, _BASE - timedelta(minutes=_SLOT_MINUTES - 1), _SLOT_MINUTES
        )
        is True
    )

    # Exactly the slot apart is allowed, on either side (boundary: allowed).
    assert (
        windows_overlap(_BASE, _BASE + timedelta(minutes=_SLOT_MINUTES), _SLOT_MINUTES)
        is False
    )
    assert (
        windows_overlap(_BASE, _BASE - timedelta(minutes=_SLOT_MINUTES), _SLOT_MINUTES)
        is False
    )

    # Zero gap (both showtimes start at the same instant) conflicts.
    assert windows_overlap(_BASE, _BASE, _SLOT_MINUTES) is True

    # Symmetric: swapping the arguments never changes the answer.
    one_short = _BASE + timedelta(minutes=_SLOT_MINUTES - 1)
    assert windows_overlap(_BASE, one_short, _SLOT_MINUTES) == windows_overlap(
        one_short, _BASE, _SLOT_MINUTES
    )


def test_day_bounds_utc_tbilisi() -> None:
    """Test 2: Tbilisi day 2026-10-12 maps to 2026-10-11T20:00Z .. 2026-10-12T20:00Z."""
    start, end = day_bounds_utc(date(2026, 10, 12), "Asia/Tbilisi")

    assert start == datetime(2026, 10, 11, 20, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 12, 20, 0, tzinfo=UTC)

    # Tbilisi has no daylight saving, so the window is exactly 24 hours.
    assert end - start == timedelta(hours=24)
    assert start.tzinfo is UTC
    assert end.tzinfo is UTC


def test_day_bounds_utc_handles_dst_day() -> None:
    """Test 3: Europe/Berlin spring-forward day is 23h and fall-back day is 25h."""
    # 2026-03-29: clocks jump 02:00 -> 03:00 local, a 23-hour day.
    spring_start, spring_end = day_bounds_utc(date(2026, 3, 29), "Europe/Berlin")
    assert spring_end - spring_start == timedelta(hours=23)

    # 2026-10-25: clocks fall back 03:00 -> 02:00 local, a 25-hour day.
    fall_start, fall_end = day_bounds_utc(date(2026, 10, 25), "Europe/Berlin")
    assert fall_end - fall_start == timedelta(hours=25)

    # Every bound is a UTC-aware, correctly ordered instant.
    for bound in (spring_start, spring_end, fall_start, fall_end):
        assert bound.tzinfo is UTC
        assert bound.utcoffset() == timedelta(0)
    assert spring_start < spring_end
    assert fall_start < fall_end


def test_conflict_window_and_naive_rejection() -> None:
    """Test conflict_window bounds and rejection of naive datetimes."""
    lower, upper = conflict_window(_BASE, _SLOT_MINUTES)
    assert lower == _BASE - timedelta(minutes=_SLOT_MINUTES)
    assert upper == _BASE + timedelta(minutes=_SLOT_MINUTES)

    naive = datetime(2026, 10, 12, 12, 0)  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError):
        conflict_window(naive, _SLOT_MINUTES)
    with pytest.raises(ValueError):
        windows_overlap(naive, _BASE, _SLOT_MINUTES)
    with pytest.raises(ValueError):
        windows_overlap(_BASE, naive, _SLOT_MINUTES)
