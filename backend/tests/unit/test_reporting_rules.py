"""Unit tests for the pure reporting rules (SPEC-6 Section 5, tests 1, 3 and 4).

No database and no HTTP are involved: every function under test is called
directly with plain values. Test 2 (the query-parameter models) lives in
``test_reporting_schemas.py`` because it exercises Pydantic, not these rules.

The date arithmetic is pinned to fixed dates so the expectations do not depend
on the machine clock or on today's date.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.reporting.rules import (
    INVALID_DATE_RANGE,
    InvalidDateRangeError,
    available_seats,
    range_bounds_utc,
    revenue_totals,
    validate_date_range,
)
from src.reporting.schemas import RevenueRowDay, RevenueRowMovie

# The maximum span of a `group_by=day` revenue report (SPEC-6 FR-6).
_MAX_GROUP_DAYS = 366

# A single fixed local day, used wherever one date is enough.
_ONE_DAY = date(2026, 10, 12)


def test_date_range_validation() -> None:
    """Test 1: reversed rejected, equal accepted, both required, 366 vs 367."""
    # FR-3: date_from after date_to is unusable, in a narrow or a wide range,
    # and the code the service answers with is defined once.
    assert INVALID_DATE_RANGE == "INVALID_DATE_RANGE"
    assert issubclass(InvalidDateRangeError, ValueError)
    with pytest.raises(InvalidDateRangeError):
        validate_date_range(date(2026, 10, 12), date(2026, 10, 11))
    with pytest.raises(InvalidDateRangeError):
        validate_date_range(date(2026, 10, 12), date(2025, 10, 12))

    # The same day on both ends is a valid one-day range.
    validate_date_range(_ONE_DAY, _ONE_DAY)
    # Ordering is checked by the two ends only: an open side is never reversed.
    validate_date_range(None, _ONE_DAY)
    validate_date_range(_ONE_DAY, None)
    validate_date_range(None, None)

    # group_by=day needs both dates: either side missing is rejected.
    with pytest.raises(InvalidDateRangeError):
        validate_date_range(None, _ONE_DAY, require_both_max_days=_MAX_GROUP_DAYS)
    with pytest.raises(InvalidDateRangeError):
        validate_date_range(_ONE_DAY, None, require_both_max_days=_MAX_GROUP_DAYS)
    with pytest.raises(InvalidDateRangeError):
        validate_date_range(None, None, require_both_max_days=_MAX_GROUP_DAYS)

    # The span is inclusive: 2024 is a leap year, so 2024-01-01..2024-12-31 is
    # exactly 366 days and is accepted; one day more is rejected.
    assert (date(2024, 12, 31) - date(2024, 1, 1)).days + 1 == 366
    validate_date_range(
        date(2024, 1, 1), date(2024, 12, 31), require_both_max_days=_MAX_GROUP_DAYS
    )
    with pytest.raises(InvalidDateRangeError):
        validate_date_range(
            date(2024, 1, 1), date(2025, 1, 1), require_both_max_days=_MAX_GROUP_DAYS
        )

    # Without the maximum the other reports stay unbounded, as their dates are.
    validate_date_range(date(2020, 1, 1), date(2026, 1, 1))


def test_range_bounds_utc_tbilisi_and_dst_zone() -> None:
    """Test 3: inclusive local days become a half-open UTC window (FR-3)."""
    # One Tbilisi day (UTC+4, no daylight saving): the window is exactly 24h and
    # starts at the local midnight that is 20:00 UTC the evening before.
    start, end = range_bounds_utc(_ONE_DAY, _ONE_DAY, "Asia/Tbilisi")
    assert start == datetime(2026, 10, 11, 20, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
    assert start is not None
    assert end is not None
    assert end - start == timedelta(hours=24)

    # A multi-day range is inclusive at both ends: 12th..14th is three days, so
    # the exclusive end is the local midnight that begins the 15th.
    start, end = range_bounds_utc(
        date(2026, 10, 12), date(2026, 10, 14), "Asia/Tbilisi"
    )
    assert start == datetime(2026, 10, 11, 20, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 14, 20, 0, tzinfo=UTC)

    # A DST zone: Europe/Berlin springs forward on 2026-03-29, so the two-day
    # range 28th..29th is 47 hours long, not 48, and both ends stay UTC-aware.
    start, end = range_bounds_utc(date(2026, 3, 28), date(2026, 3, 29), "Europe/Berlin")
    assert start == datetime(2026, 3, 27, 23, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)
    assert start is not None
    assert end is not None
    assert end - start == timedelta(hours=47)
    assert start.tzinfo is UTC
    assert end.tzinfo is UTC

    # A missing date means unbounded on that side; both missing is no window.
    assert range_bounds_utc(None, None, "Asia/Tbilisi") == (None, None)
    start, end = range_bounds_utc(_ONE_DAY, None, "Asia/Tbilisi")
    assert start == datetime(2026, 10, 11, 20, 0, tzinfo=UTC)
    assert end is None
    start, end = range_bounds_utc(None, _ONE_DAY, "Asia/Tbilisi")
    assert start is None
    assert end == datetime(2026, 10, 12, 20, 0, tzinfo=UTC)


def test_revenue_totals_equal_sum_of_rows_and_available_is_never_negative() -> None:
    """Test 4: totals are exactly the row sums; available seats clamp at 0."""
    # FR-6: the totals of either grouping are the sums of the rows themselves.
    movie_rows: list[RevenueRowMovie] = [
        RevenueRowMovie(movie_id=2, title="Second", tickets_sold=3, revenue_cents=3000),
        RevenueRowMovie(movie_id=1, title="First", tickets_sold=5, revenue_cents=1250),
    ]
    assert revenue_totals(movie_rows) == (4250, 8)

    day_rows: list[RevenueRowDay] = [
        RevenueRowDay(date=date(2026, 10, 12), tickets_sold=2, revenue_cents=900),
        RevenueRowDay(date=date(2026, 10, 13), tickets_sold=1, revenue_cents=450),
    ]
    assert revenue_totals(day_rows) == (1350, 3)

    # Both row types satisfy the same protocol, so they can be summed together
    # and the totals agree with a hand sum over the very same rows.
    mixed: list[RevenueRowMovie | RevenueRowDay] = [*movie_rows, *day_rows]
    total_cents, tickets_sold = revenue_totals(mixed)
    assert total_cents == sum(row.revenue_cents for row in mixed) == 5600
    assert tickets_sold == sum(row.tickets_sold for row in mixed) == 11
    assert isinstance(total_cents, int)
    assert isinstance(tickets_sold, int)

    # An empty range is not an error: zero totals and no rows (FR-6).
    assert revenue_totals([]) == (0, 0)

    # FR-4: available = total - sold - held, and never a negative number.
    assert available_seats(100, 10, 5) == 85
    assert available_seats(100, 100, 0) == 0
    assert available_seats(100, 0, 100) == 0
    assert available_seats(0, 0, 0) == 0
    # A logically impossible input (sold + held > total) clamps at 0 instead of
    # reporting a negative seat count.
    assert available_seats(10, 7, 7) == 0
    assert isinstance(available_seats(10, 7, 7), int)
