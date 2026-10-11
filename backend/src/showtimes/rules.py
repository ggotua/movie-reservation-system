"""Pure scheduling arithmetic for showtimes (SPEC-3 Sections 2.5 and 11).

No database and no HTTP: every function here takes plain values and returns
plain values, so the overlap rule and the browse-by-date window can be tested
without a connection or a live request (SPEC-3 Section 11, Single
Responsibility Check).

All instants are timezone-aware UTC values (SPEC-3 FR-15). The two functions
that take instants reject a naive ``datetime`` with :class:`ValueError`, because
a naive value would make the comparison depend on the machine's local timezone
instead of the stored instant.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def _require_aware(value: datetime, name: str) -> None:
    """Reject a naive ``datetime`` before it reaches the arithmetic.

    Args:
        value: The instant to check.
        name: The parameter name, used in the error message.

    Raises:
        ValueError: If ``value`` carries no timezone information. A value whose
            ``tzinfo`` is set but whose ``utcoffset()`` returns ``None`` (a
            misbehaving custom tzinfo) is treated as naive too.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def conflict_window(start: datetime, slot_minutes: int) -> tuple[datetime, datetime]:
    """Return the exclusive window that conflicts with ``start``.

    A showtime at ``other`` conflicts with ``start`` exactly when ``other`` is
    strictly inside ``(start - slot, start + slot)``; both bounds themselves are
    allowed, so a showtime exactly ``slot`` minutes away is not a conflict
    (SPEC-3 FR-11).

    Args:
        start: Timezone-aware start instant of the new showtime.
        slot_minutes: Length of the conflict half-window in minutes
            (``SHOWTIME_SLOT_MINUTES``).

    Returns:
        tuple[datetime, datetime]: ``(start - slot, start + slot)``, intended as
        the exclusive lower and upper bounds of an overlap query.

    Raises:
        ValueError: If ``start`` is naive (see :func:`_require_aware`).
    """
    _require_aware(start, "start")
    slot = timedelta(minutes=slot_minutes)
    return (start - slot, start + slot)


def windows_overlap(a: datetime, b: datetime, slot_minutes: int) -> bool:
    """Return whether two showtimes on one screen conflict.

    Conflict means the two start instants are strictly less than
    ``slot_minutes`` apart. Exactly ``slot_minutes`` apart is allowed (the
    boundary falls on the "allowed" side), and the test is symmetric: the result
    does not depend on argument order (SPEC-3 FR-11, test 1).

    Args:
        a: First timezone-aware start instant.
        b: Second timezone-aware start instant.
        slot_minutes: Minimum allowed separation in minutes
            (``SHOWTIME_SLOT_MINUTES``).

    Returns:
        bool: ``True`` when ``abs(a - b) < slot_minutes``, otherwise ``False``.

    Raises:
        ValueError: If ``a`` or ``b`` is naive (see :func:`_require_aware`).
    """
    _require_aware(a, "a")
    _require_aware(b, "b")
    return abs(a - b) < timedelta(minutes=slot_minutes)


def day_bounds_utc(day: date, tz_name: str) -> tuple[datetime, datetime]:
    """Return the UTC bounds of a calendar day in a given timezone.

    The window is half-open: local midnight of ``day`` (inclusive) to local
    midnight of the following day (exclusive), both converted to UTC. Because
    the bounds are taken at local midnight and then converted, the window is
    correct on daylight-saving transition days -- a 23- or 25-hour local day
    yields a UTC window of the matching length (SPEC-3 FR-13, D2; test 3).

    Args:
        day: The calendar day as it is understood in ``tz_name``.
        tz_name: An IANA timezone name, e.g. ``"Asia/Tbilisi"``. Validated when
            settings load, so an unknown name here indicates a programming
            error rather than user input.

    Returns:
        tuple[datetime, datetime]: ``(start_utc, end_utc)`` as timezone-aware
        UTC datetimes, with ``start_utc < end_utc``.

    Raises:
        zoneinfo.ZoneInfoNotFoundError: If ``tz_name`` is not a known IANA
            timezone name.
        ValueError: If ``tz_name`` is malformed (e.g. contains a path
            separator), as raised by :class:`zoneinfo.ZoneInfo`.
    """
    tz = ZoneInfo(tz_name)
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return (start_local.astimezone(UTC), end_local.astimezone(UTC))
