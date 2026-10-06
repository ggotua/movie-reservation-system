"""Shared enum vocabulary for the Movie Reservation System.

Defines the role / status / seat-type vocabularies exactly once so that the
SQLAlchemy Core table definitions (:mod:`src.db.tables`) and the later module
business logic both import the same values instead of restating bare string
literals (SPEC-1 Section 9, DRY check).

Each vocabulary is a parametrised :data:`typing.Literal` (usable in function
signatures and Pydantic models) paired with a plain tuple of its runtime
values (usable in SQL ``IN`` lists and CHECK constraints). The tuples are
derived from the ``Literal`` types via :func:`typing.get_args`, so the allowed
values are declared in exactly one place.

Per SPEC-1 Section 2.9, ``reservations.status`` and
``seat_reservations.status`` intentionally share one vocabulary
(:data:`ReservationStatus`); :data:`ACTIVE_RESERVATION_STATUSES` is the subset
the partial unique index keys on.
"""

from __future__ import annotations

from typing import Literal, get_args

# --- users.role (SPEC-1 Section 2.1) --------------------------------------
UserRole = Literal["user", "admin"]

USER_ROLES: tuple[UserRole, ...] = get_args(UserRole)
DEFAULT_USER_ROLE: UserRole = "user"

# --- reservations.status / seat_reservations.status (SPEC-1 2.8 / 2.9) -----
ReservationStatus = Literal["held", "confirmed", "cancelled", "expired"]

RESERVATION_STATUSES: tuple[ReservationStatus, ...] = get_args(ReservationStatus)
DEFAULT_RESERVATION_STATUS: ReservationStatus = "held"

# The "active" subset that holds a seat; the partial unique index on
# seat_reservations keys on exactly these values (FR-2 / FR-3).
ACTIVE_RESERVATION_STATUSES: tuple[ReservationStatus, ...] = (
    "held",
    "confirmed",
)

# --- seats.seat_type (SPEC-1 Section 2.6) ----------------------------------
SeatType = Literal["standard", "accessible"]

SEAT_TYPES: tuple[SeatType, ...] = get_args(SeatType)
DEFAULT_SEAT_TYPE: SeatType = "standard"
