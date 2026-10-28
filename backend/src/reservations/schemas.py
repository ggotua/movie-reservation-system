"""Pydantic request and response models for the reservation endpoints.

The SPEC-4 models are the wire formats fixed by SPEC-4 Section 2.3. SPEC-5 adds
:class:`ShowtimeSummary`, :class:`ReservationListItem` and :class:`ListParams`
for the reservation list without changing ``ReservationOut`` or any other
SPEC-4 schema (SPEC-5 D5). They only shape and validate: the seat cap,
showtime/seat existence, ownership, expiry, filtering and the ``total_cents``
calculation all live in :mod:`src.reservations.service`, so this module imports
nothing from it and no response model carries a field the SPEC does not list.

Input rules implemented here, all from SPEC-4 Section 2.3 and FR-3:

* :class:`HoldRequest` is ``extra="forbid"``, so an unknown key is a validation
  error;
* ``seat_ids`` must hold at least one id and every id must be a *strict* integer
  -- a float such as ``1.0`` or a numeric string such as ``"1"`` is rejected
  instead of coerced;
* ``seat_ids`` must be distinct;
* ``showtime_id`` is a strict integer for the same reason;
* the upper bound ``MAX_SEATS_PER_HOLD`` is deliberately **not** applied here:
  it is a service rule that answers 422 ``TOO_MANY_SEATS``, not a shape rule.

Money is always integer cents (``principles.md``):
:attr:`ReservationOut.total_cents` is an ``int`` and is computed by the service
as ``price_cents * seat count`` (FR-12), never stored.

Import direction (SPEC-4 Section 11.1): ``reservations`` may import ``core`` but
not ``showtimes`` or ``movies``, so :class:`ScreenRef` is declared here with the
fields the seat map needs rather than imported from another module's schemas.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.enums import ReservationStatus, SeatType
from src.core.pagination import PageParams
from src.reservations.rules import SeatState


class HoldRequest(BaseModel):
    """The body of ``POST /reservations`` (SPEC-4 Section 2.3, FR-3).

    Extra fields are forbidden. The number of seats is only lower-bounded here;
    the configured upper bound is a service rule (``MAX_SEATS_PER_HOLD``, 422
    ``TOO_MANY_SEATS``), so this model accepts any non-empty distinct strict-int
    list.

    Attributes:
        showtime_id: The showtime the seats belong to. A strict integer: a
            float or numeric string is rejected rather than coerced.
        seat_ids: Distinct seat ids to hold, at least one. Every entry is a
            strict integer.
    """

    model_config = ConfigDict(extra="forbid")

    showtime_id: Annotated[int, Field(strict=True)]
    seat_ids: Annotated[
        list[Annotated[int, Field(strict=True)]],
        Field(min_length=1),
    ]

    @field_validator("seat_ids")
    @classmethod
    def _require_distinct_seat_ids(cls, value: list[int]) -> list[int]:
        """Reject a ``seat_ids`` list that repeats an id (FR-3).

        Ordering is not changed: the service sorts the ids before inserting so
        every transaction takes locks in the same order, but the request's
        meaning does not depend on order.

        Args:
            value: The validated list of integer seat ids.

        Returns:
            list[int]: The list unchanged when every id is distinct.

        Raises:
            ValueError: If any id appears more than once.
        """
        if len(set(value)) != len(value):
            raise ValueError("seat_ids must be distinct")
        return value


class SeatRef(BaseModel):
    """One seat of a reservation, ordered by row label then seat number.

    Attributes:
        seat_id: The seat's primary key.
        row_label: The seat's row label, e.g. ``"A"``.
        seat_number: The seat's number within its row.
    """

    seat_id: int
    row_label: str
    seat_number: int


class SeatOut(SeatRef):
    """One seat of the public seat map (SPEC-4 Section 2.3, FR-10).

    Attributes:
        seat_id: The seat's primary key.
        row_label: The seat's row label, e.g. ``"A"``.
        seat_number: The seat's number within its row.
        seat_type: ``standard`` or ``accessible``.
        state: Derived state: ``available``, ``held`` (an unexpired hold) or
            ``taken`` (confirmed). An expired hold is reported as
            ``available``; the holder is never revealed (D6).
    """

    seat_type: SeatType
    state: SeatState


class ReservationOut(BaseModel):
    """A reservation as the API returns it (SPEC-4 Section 2.3).

    ``status`` is the *effective* status: a ``held`` row past its expiry is
    reported as ``expired`` without writing to the database (Section 2.5,
    "Reads never write").

    Attributes:
        id: The reservation's primary key.
        showtime_id: The showtime the reservation belongs to.
        status: The effective status after applying lazy expiry.
        seats: The reserved seats, ordered by row label then seat number.
        total_cents: ``showtime.price_cents * len(seats)``, integer cents
            (FR-12). Computed by the service, never stored.
        created_at: When the hold was created, in UTC.
        expires_at: ``created_at + HOLD_MINUTES`` for a ``held`` reservation;
            ``None`` for every other status.
        confirmed_at: When the reservation was confirmed, or ``None``.
        cancelled_at: When the hold was released, or ``None``.
    """

    id: int
    showtime_id: int
    status: ReservationStatus
    seats: list[SeatRef]
    total_cents: int
    created_at: datetime
    expires_at: datetime | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None


class MovieSummary(BaseModel):
    """The movie embedded in a reservation list item (SPEC-5 Section 2.3).

    Declared here rather than imported from :mod:`src.movies`: the
    ``reservations`` module must not import the ``movies`` module (SPEC-4
    Section 11.1).

    Attributes:
        id: The movie's primary key.
        title: The stored movie title.
        poster_url: The poster image URL, or ``None`` when no poster is stored
            (the column is nullable in SPEC-1 Section 2.3).
    """

    id: int
    title: str
    poster_url: str | None


class ScreenSummary(BaseModel):
    """The screen embedded in a reservation list item (SPEC-5 Section 2.3).

    Only the identity is needed by the list, unlike :class:`ScreenRef` which
    also carries the seat-map dimensions.

    Attributes:
        id: The screen's primary key.
        name: The stored screen name.
    """

    id: int
    name: str


class ShowtimeSummary(BaseModel):
    """The showtime embedded in a reservation list item (SPEC-5 D5).

    Attributes:
        id: The showtime's primary key.
        starts_at: When the showtime starts, in UTC.
        price_cents: The showtime's ticket price in integer cents
            (``principles.md``: money is never a float).
        movie: The movie being shown.
        screen: The screen the showtime runs on.
    """

    id: int
    starts_at: datetime
    price_cents: int
    movie: MovieSummary
    screen: ScreenSummary


class ReservationListItem(ReservationOut):
    """One reservation in the caller's list, with its showtime embedded.

    It has every field of :class:`ReservationOut` -- unchanged, so the SPEC-4
    response shape stays fixed (SPEC-5 D5) -- plus the nested ``showtime``. As
    in :class:`ReservationOut`, ``status`` is the effective status: a ``held``
    row past its expiry is reported as ``expired`` without a write.

    Attributes:
        showtime: The showtime, movie and screen the reservation belongs to.
    """

    showtime: ShowtimeSummary


class ListParams(BaseModel):
    """The validated query of ``GET /reservations`` (SPEC-5 Section 2.3, D4).

    ``page`` reuses the shared :class:`~src.core.pagination.PageParams` (and
    therefore :func:`~src.core.pagination.get_page_params`), so the
    ``limit``/``offset`` bounds are defined in exactly one place instead of a
    second pagination implementation.

    Attributes:
        status: Optional filter on the *effective* status, one of ``held``,
            ``confirmed``, ``cancelled`` or ``expired``. Omitted means no
            status filter. A ``held`` row past its expiry matches ``expired``
            (SPEC-5 FR-3); the filter itself is applied by the service.
        upcoming: Optional filter on the showtime start (SPEC-5 FR-4):
            ``True`` keeps showtimes that start after ``now``, ``False`` keeps
            showtimes that have started (``starts_at <= now``), and omitted
            keeps both. Query strings such as ``"true"``/``"false"`` are parsed
            into booleans; anything else is a validation error.
        page: The validated pagination window: ``limit`` 1-100 (default 20)
            and ``offset`` 0 or greater.
    """

    model_config = ConfigDict(extra="forbid")

    status: ReservationStatus | None = None
    upcoming: bool | None = None
    page: PageParams


class ScreenRef(BaseModel):
    """The screen reference embedded in :class:`SeatMapOut` (Section 2.3).

    Attributes:
        id: The screen's primary key.
        name: The stored screen name.
        rows: Number of seat rows.
        seats_per_row: Seats in each row.
    """

    id: int
    name: str
    rows: int
    seats_per_row: int


class SeatMapOut(BaseModel):
    """The public seat map of one showtime (SPEC-4 Section 2.3, D6).

    Attributes:
        showtime_id: The showtime the map belongs to.
        screen: The screen's identity and dimensions.
        seats: Every seat of the screen with its derived state, never who
            holds it.
    """

    showtime_id: int
    screen: ScreenRef
    seats: list[SeatOut]
