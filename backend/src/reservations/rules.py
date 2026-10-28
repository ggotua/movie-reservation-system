"""Pure hold-expiry, effective-status and seat-state logic for reservations.

No database and no HTTP: every function here takes plain values and returns a
plain value, so the hold window, the status the API reports and the seat-map
derivation can be tested without a connection or a live request (SPEC-4
Sections 2.5 and 11, SPEC-5 Sections 2.5 and 11, Single Responsibility Check).

All instants are timezone-aware UTC values (SPEC-4 FR-5). Every function that
takes an instant rejects a naive ``datetime`` with :class:`ValueError`, because
a naive value would make the comparison depend on the machine's local timezone
instead of the stored instant (the same rule as :mod:`src.showtimes.rules`).

A hold has no expiry column: its expiry is derived from ``created_at`` and
``HOLD_MINUTES`` (SPEC-4 D2). :func:`hold_expires_at` and :func:`hold_cutoff`
are the two forms of that single calculation -- Python compares against the
former, SQL selects on the latter -- and :func:`is_hold_expired` is the one
boundary definition used by the service.

:func:`effective_status` is the one definition of the status a stored
reservation row is reported as (SPEC-5 FR-3): a ``held`` row past its expiry
reads as ``expired`` while the stored row is left untouched ("reads never
write", SPEC-4 Section 2.5). The reservation read-back in
:mod:`src.reservations.service` calls it instead of repeating the condition.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from src.core.enums import (
    DEFAULT_RESERVATION_STATUS,
    RESERVATION_STATUSES,
    ReservationStatus,
)

# The three states the public seat map reports (SPEC-4 Section 2.3, FR-10).
SeatState = Literal["available", "held", "taken"]

# The statuses :func:`effective_status` compares, taken from the single
# vocabulary in ``core/enums.py`` (SPEC-4 Section 10, DRY check): a new
# reservation starts as ``held``, and ``RESERVATION_STATUSES`` declares the four
# statuses in the order held, confirmed, cancelled, expired.
_HELD_STATUS: ReservationStatus = DEFAULT_RESERVATION_STATUS
_EXPIRED_STATUS: ReservationStatus = RESERVATION_STATUSES[-1]


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


def hold_expires_at(created_at: datetime, hold_minutes: int) -> datetime:
    """Return the instant at which a hold stops blocking its seats (FR-5).

    The expiry is derived, never stored: it is always
    ``created_at + HOLD_MINUTES`` (SPEC-4 D2).

    Args:
        created_at: Timezone-aware instant at which the hold was created.
        hold_minutes: The configured hold window (``HOLD_MINUTES``).

    Returns:
        datetime: ``created_at + hold_minutes``, timezone-aware.

    Raises:
        ValueError: If ``created_at`` is naive (see :func:`_require_aware`).
    """
    _require_aware(created_at, "created_at")
    return created_at + timedelta(minutes=hold_minutes)


def is_hold_expired(created_at: datetime, now: datetime, hold_minutes: int) -> bool:
    """Return whether a hold has reached its expiry instant (FR-5).

    The boundary is inclusive on the expired side: a hold whose
    ``created_at + HOLD_MINUTES`` equals ``now`` is expired, not active. This
    is the definition used by confirm/release, so a hold is never both
    "active" and "confirmable" at the same instant (SPEC-4 Section 3).

    Args:
        created_at: Timezone-aware instant at which the hold was created.
        now: Timezone-aware instant to compare against. The caller passes one
            value per request so every check in that request agrees.
        hold_minutes: The configured hold window (``HOLD_MINUTES``).

    Returns:
        bool: ``True`` when ``now >= created_at + hold_minutes``, otherwise
        ``False``.

    Raises:
        ValueError: If ``created_at`` or ``now`` is naive (see
            :func:`_require_aware`).
    """
    _require_aware(now, "now")
    return now >= hold_expires_at(created_at, hold_minutes)


def hold_cutoff(now: datetime, hold_minutes: int) -> datetime:
    """Return the SQL cutoff for expired held rows (FR-5).

    A held row is expired exactly when ``created_at <= hold_cutoff(now, hold)``,
    which is algebraically the same condition as
    ``now >= created_at + hold``. The service passes this value to the lazy
    expiry query, so the database and Python share one boundary.

    Args:
        now: Timezone-aware instant representing the current time.
        hold_minutes: The configured hold window (``HOLD_MINUTES``).

    Returns:
        datetime: ``now - hold_minutes``, timezone-aware.

    Raises:
        ValueError: If ``now`` is naive (see :func:`_require_aware`).
    """
    _require_aware(now, "now")
    return now - timedelta(minutes=hold_minutes)


def effective_status(
    stored_status: ReservationStatus,
    created_at: datetime,
    now: datetime,
    hold_minutes: int,
) -> ReservationStatus:
    """Derive the status a reservation is reported with (SPEC-5 FR-3).

    This is the one definition of the lazy-expiry rule on reads: a stored
    ``held`` row whose hold has expired (``is_hold_expired``, with the expiry
    instant itself counting as expired) is reported as ``expired``. Every other
    stored status -- ``confirmed``, ``cancelled`` and a stored ``expired`` -- is
    returned unchanged, because only a hold can expire. Nothing is written:
    reads compute the status instead of updating rows (SPEC-4 Section 2.5).

    Args:
        stored_status: The status stored on the reservation row.
        created_at: ``created_at`` of the reservation, used as the start of the
            hold window. Not used for statuses other than ``held``, but still
            required because every row has one.
        now: Timezone-aware instant representing the current time. The caller
            passes one value per request so every check in that request agrees.
        hold_minutes: The configured hold window (``HOLD_MINUTES``).

    Returns:
        ReservationStatus: ``expired`` when ``stored_status`` is ``held`` and
        the hold has expired; otherwise ``stored_status`` unchanged.

    Raises:
        ValueError: If ``created_at`` or ``now`` is naive, or if
            ``stored_status`` is not one of the shared reservation statuses --
            an unknown value would otherwise be reported to a client as-is.
    """
    _require_aware(created_at, "created_at")
    _require_aware(now, "now")
    if stored_status not in RESERVATION_STATUSES:
        raise ValueError(f"unknown reservation status: {stored_status!r}")

    if stored_status == _HELD_STATUS and is_hold_expired(created_at, now, hold_minutes):
        return _EXPIRED_STATUS
    return stored_status


def seat_state(
    active_status: str | None,
    hold_created_at: datetime | None,
    now: datetime,
    hold_minutes: int,
) -> SeatState:
    """Derive the public state of one seat for the seat map (FR-10).

    This is the one place the mapping from stored statuses to
    ``available | held | taken`` is defined. An expired ``held`` row is
    reported as ``available`` and is never written by this function: reads
    compute expiry instead of updating rows (SPEC-4 Section 2.5).

    Args:
        active_status: Status of the seat's active reservation row, or ``None``
            when the seat has no active row (no row at all, or only
            ``cancelled``/``expired`` rows).
        hold_created_at: ``created_at`` of the row when ``active_status`` is
            ``"held"``; ignored otherwise. Required for a held row because
            expiry cannot be derived without it.
        now: Timezone-aware instant representing the current time.
        hold_minutes: The configured hold window (``HOLD_MINUTES``).

    Returns:
        SeatState: ``"taken"`` for ``confirmed``; ``"held"`` for an unexpired
        ``held`` row; ``"available"`` otherwise (no row, expired hold,
        ``cancelled`` or ``expired``).

    Raises:
        ValueError: If ``now`` is naive, if ``hold_created_at`` is supplied but
            naive, if ``active_status`` is ``"held"`` without a
            ``hold_created_at``, or if ``active_status`` is not one of the
            shared reservation statuses (FR-11) -- an unknown value would
            otherwise be silently reported as available.
    """
    _require_aware(now, "now")
    if hold_created_at is not None:
        _require_aware(hold_created_at, "hold_created_at")

    if active_status is None:
        return "available"
    if active_status not in RESERVATION_STATUSES:
        raise ValueError(f"unknown reservation status: {active_status!r}")

    if active_status == "confirmed":
        return "taken"
    if active_status == "held":
        if hold_created_at is None:
            raise ValueError("hold_created_at is required when active_status is 'held'")
        return (
            "available"
            if is_hold_expired(hold_created_at, now, hold_minutes)
            else "held"
        )
    # Only "cancelled" and "expired" remain; neither blocks the seat.
    return "available"
