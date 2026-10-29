"""Pure rules of the admin reporting endpoints (SPEC-6 FR-3, FR-4 and FR-6).

Every function here is a pure calculation: plain values in, plain values out.
This module opens no connection, builds no SQL, reads no clock and touches no
HTTP, so it can be unit-tested without a database and reused by the service
without duplicating a rule.

The three rules it owns:

* the *date range*: ``date_from``/``date_to`` are inclusive local calendar days
  of the showtime in ``CINEMA_TIMEZONE`` (FR-3), and ``group_by=day`` needs a
  complete range of at most 366 days (FR-6);
* the *UTC window* those days map to, derived from
  :func:`src.showtimes.rules.day_bounds_utc` rather than reimplemented
  (SPEC-6 Section 2.5);
* the *derived numbers*: ``available_seats`` (FR-4) and the row totals of a
  revenue report (FR-6).

A bad range is reported as :class:`InvalidDateRangeError`, a ``ValueError``
subclass, so the service can map it to the single ``INVALID_DATE_RANGE`` code
(HTTP 422, SPEC-6 Section 2.4) in one place instead of at every call site.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from src.showtimes.rules import day_bounds_utc

#: The error code for an unusable report date range (SPEC-6 Section 2.4). It is
#: defined once, here, and imported by the reporting service, which turns
#: :class:`InvalidDateRangeError` into a 422 response with this code
#: (``INVALID_DATE_RANGE``: ``date_from > date_to``, a ``group_by=day`` request
#: without both dates, or a range longer than the maximum).
INVALID_DATE_RANGE = "INVALID_DATE_RANGE"


class InvalidDateRangeError(ValueError):
    """An unusable report date range (mapped to 422 ``INVALID_DATE_RANGE``).

    It subclasses :class:`ValueError` because the range is a domain-rule
    violation of an otherwise well-formed request: the values parsed fine, they
    just do not describe a range a report can be asked for. The service catches
    this type and answers with :data:`INVALID_DATE_RANGE` (SPEC-6 Section 2.4,
    FR-12), keeping the message raised here as the error message.
    """


class RevenueRow(Protocol):
    """The two fields a revenue row must expose for a report total (FR-6).

    Both :class:`~src.reporting.schemas.RevenueRowMovie` and
    :class:`~src.reporting.schemas.RevenueRowDay` satisfy it, so the totals are
    summed by one function whichever grouping was requested.

    Attributes:
        tickets_sold: Confirmed seats counted by the row.
        revenue_cents: Money of the row, in integer cents (``principles.md``:
            money is never a float).
    """

    tickets_sold: int
    revenue_cents: int


def validate_date_range(
    date_from: date | None,
    date_to: date | None,
    *,
    require_both_max_days: int | None = None,
) -> None:
    """Validate a report's date range against FR-3 and the FR-6 day rule.

    Args:
        date_from: First inclusive local day, or ``None`` for "no lower bound".
        date_to: Last inclusive local day, or ``None`` for "no upper bound".
        require_both_max_days: When given, both dates become mandatory and the
            range, counted inclusively, may span at most this many days. Pass
            the maximum only for ``group_by=day`` (FR-6); leave it ``None`` for
            the other reports, whose dates are optional and unbounded.

    Returns:
        None: The function is called for its check, not for a value.

    Raises:
        InvalidDateRangeError: If ``date_from`` is after ``date_to`` (FR-3); if
            ``require_both_max_days`` was given and either date is missing; or
            if the inclusive span exceeds ``require_both_max_days``. A one-day
            range (``date_from == date_to``) is valid and counts as one day, so
            a 366-day range is accepted and a 367-day range is rejected.
    """
    if date_from is not None and date_to is not None and date_from > date_to:
        raise InvalidDateRangeError(
            f"date_from ({date_from.isoformat()}) must not be after "
            f"date_to ({date_to.isoformat()})"
        )

    if require_both_max_days is None:
        return

    if date_from is None or date_to is None:
        raise InvalidDateRangeError("group_by=day requires both date_from and date_to")

    # Inclusive span: a single day is 1 and both ends are counted, so the
    # boundary is exactly `require_both_max_days` days (366 accepted).
    span_days = (date_to - date_from).days + 1
    if span_days > require_both_max_days:
        raise InvalidDateRangeError(
            f"date range spans {span_days} days; the maximum is {require_both_max_days}"
        )


def range_bounds_utc(
    date_from: date | None,
    date_to: date | None,
    tz_name: str,
) -> tuple[datetime | None, datetime | None]:
    """Map inclusive local days to the half-open UTC window a report filters on.

    ``start`` is the local midnight that begins ``date_from`` and ``end`` is the
    local midnight that begins the day *after* ``date_to``, both converted to
    UTC, so a showtime late on ``date_to`` still falls inside ``[start, end)``
    while one at 00:00 of the following local day does not (FR-3). The
    conversion reuses :func:`src.showtimes.rules.day_bounds_utc`, which already
    handles a zone whose day is 23 or 25 hours long, so no timezone arithmetic
    is repeated here (SPEC-6 Section 2.5).

    Call :func:`validate_date_range` first: this function only converts, it
    does not reject a reversed range, because one rule must have one
    definition.

    Args:
        date_from: First inclusive local day, or ``None`` for "unbounded below".
        date_to: Last inclusive local day, or ``None`` for "unbounded above".
        tz_name: IANA name of the cinema's timezone (``CINEMA_TIMEZONE``).

    Returns:
        tuple[datetime | None, datetime | None]: ``(start, end)``, UTC-aware,
        with ``end`` exclusive. A missing date yields ``None`` on that side, so
        ``(None, None)`` means the caller applies no window at all.

    Raises:
        ValueError: If ``tz_name`` is not a known IANA timezone, propagated from
            :func:`src.showtimes.rules.day_bounds_utc`.
    """
    start = day_bounds_utc(date_from, tz_name)[0] if date_from is not None else None
    end = day_bounds_utc(date_to, tz_name)[1] if date_to is not None else None
    return start, end


def available_seats(total: int, sold: int, held: int) -> int:
    """Compute a showtime's sellable seats (SPEC-6 FR-4).

    Capacity is ``total - sold - held``, where ``sold`` counts confirmed seats
    and ``held`` counts only *unexpired* holds; cancelled, expired and stale
    reservations are excluded by the query that produces the inputs, not here.

    Args:
        total: Seats the screen has.
        sold: Seats confirmed for the showtime.
        held: Seats on an unexpired hold for the showtime.

    Returns:
        int: The remaining seats, clamped at 0. The unique seat index makes
        ``sold + held <= total`` an invariant, so the clamp guards against a
        logically impossible input instead of hiding a real overbooking.
    """
    return max(total - sold - held, 0)


def revenue_totals(rows: Sequence[RevenueRow]) -> tuple[int, int]:
    """Sum a revenue report's rows into its totals (SPEC-6 FR-6).

    The totals are computed from the very rows the response returns, so
    ``total_cents`` can never disagree with the rows, whichever way they are
    grouped.

    Args:
        rows: The grouped rows of the report, in any order. Empty is valid and
            yields zero totals (an empty range is not an error).

    Returns:
        tuple[int, int]: ``(total_cents, tickets_sold)``, both plain integers:
        money stays integer cents and is never a float.
    """
    total_cents = sum(row.revenue_cents for row in rows)
    tickets_sold = sum(row.tickets_sold for row in rows)
    return total_cents, tickets_sold
