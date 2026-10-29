"""Pydantic models of the admin reporting endpoints (SPEC-6 Section 2.3).

The wire formats fixed by SPEC-6 are declared here: the three response shapes
(:class:`AdminReservationItem`, :class:`CapacityRow`, :class:`RevenueOut` with
its two row types) and the query-parameter models that validate what a client
may ask for. They only shape and validate; every filter, aggregate, ordering
and total is computed by :mod:`src.reporting.service`, so this module imports
nothing from it.

Two rules are visible in the models themselves:

* money is always integer cents (``principles.md``): ``price_cents``,
  ``total_cents`` and ``revenue_cents`` are ``int`` and never a float;
* a user is exposed as ``id`` and ``email`` only (FR-11). The reporting models
  are declared here instead of reusing :mod:`src.reservations.schemas` because
  admin items carry the user, and no model here has a field -- password hash
  included -- that SPEC-6 Section 2.3 does not list.

Pagination reuses the shared :class:`~src.core.pagination.PageParams`, so
``limit``/``offset`` bounds are defined in exactly one place (FR-9). The
``status`` filter reuses :class:`~src.core.enums.ReservationStatus`, so the
four statuses also have one definition. Whether the dates of a request form a
usable range is a domain rule, not a shape rule, and lives in
:mod:`src.reporting.rules`.
"""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.core.enums import ReservationStatus
from src.core.pagination import PageParams

#: How a revenue report groups its rows (SPEC-6 Section 2.3): by movie
#: (default) or by the showtime's local day in ``CINEMA_TIMEZONE``.
GroupBy = Literal["movie", "day"]


class UserRef(BaseModel):
    """The user an admin reservation belongs to (SPEC-6 FR-11).

    Attributes:
        id: The user's primary key.
        email: The user's email address. It is the only other user field the
            reports expose; a password hash is never part of any response.
    """

    id: int
    email: str


class MovieRef(BaseModel):
    """The movie embedded in a reporting item (SPEC-6 Section 2.3).

    Attributes:
        id: The movie's primary key.
        title: The stored title.
    """

    id: int
    title: str


class ScreenRef(BaseModel):
    """The screen embedded in a reporting item (SPEC-6 Section 2.3).

    Attributes:
        id: The screen's primary key.
        name: The stored screen name.
    """

    id: int
    name: str


class ReportShowtimeRef(BaseModel):
    """The showtime embedded in an admin reservation or a capacity row.

    Attributes:
        id: The showtime's primary key.
        starts_at: When the showtime starts, in UTC.
        price_cents: The ticket price in integer cents (``principles.md``).
        movie: The movie being shown.
        screen: The screen the showtime runs on.
    """

    id: int
    starts_at: datetime.datetime
    price_cents: int
    movie: MovieRef
    screen: ScreenRef


class SeatRef(BaseModel):
    """One seat of an admin reservation, never who holds it (FR-11).

    Attributes:
        seat_id: The seat's primary key.
        row_label: The seat's row label on the screen.
        seat_number: The seat's number within its row.
    """

    seat_id: int
    row_label: str
    seat_number: int


class AdminReservationItem(BaseModel):
    """One reservation of any user, as an admin sees it (SPEC-6 Section 2.3).

    ``status`` is the *effective* status: a ``held`` row past its expiry is
    reported as ``expired`` without a write, using the same SQL definition as
    the SPEC-5 list (FR-10). ``expires_at`` is present for a held reservation
    and ``None`` otherwise, exactly as in the SPEC-4 shape.

    Attributes:
        id: The reservation's primary key.
        status: The effective status, one of the four shared statuses.
        created_at: When the reservation was created, in UTC.
        expires_at: When an unexpired hold lapses, or ``None`` when the
            reservation is not held.
        confirmed_at: When the reservation was confirmed, or ``None``.
        cancelled_at: When the reservation was cancelled, or ``None``.
        seats: The seats the reservation covers.
        total_cents: ``price_cents`` times the number of seats, in integer
            cents.
        user: The owner, exposed as ``id`` and ``email`` only.
        showtime: The showtime, movie and screen the reservation belongs to.
    """

    id: int
    status: ReservationStatus
    created_at: datetime.datetime
    expires_at: datetime.datetime | None
    confirmed_at: datetime.datetime | None
    cancelled_at: datetime.datetime | None
    seats: list[SeatRef]
    total_cents: int
    user: UserRef
    showtime: ReportShowtimeRef


class CapacityRow(BaseModel):
    """Capacity of one showtime (SPEC-6 Section 2.3, FR-4).

    ``available_seats`` is computed from the other three by
    :func:`src.reporting.rules.available_seats`, so a response can never claim
    more sellable seats than the subtraction allows. Cancelled, expired and
    stale-held reservations are counted in neither ``sold_seats`` nor
    ``held_seats``.

    Attributes:
        showtime: The showtime the numbers describe.
        total_seats: Seats the screen has.
        sold_seats: Seats confirmed for the showtime.
        held_seats: Seats on an unexpired hold for the showtime.
        available_seats: ``total_seats - sold_seats - held_seats``, never
            negative.
    """

    showtime: ReportShowtimeRef
    total_seats: int
    sold_seats: int
    held_seats: int
    available_seats: int


class RevenueRowMovie(BaseModel):
    """One movie's line of a revenue report (SPEC-6 Section 2.3).

    Attributes:
        movie_id: The movie's primary key.
        title: The movie title, for display without a second request.
        tickets_sold: Confirmed seats of this movie in the range.
        revenue_cents: ``tickets_sold`` times the showtimes' ``price_cents``,
            in integer cents.
    """

    movie_id: int
    title: str
    tickets_sold: int
    revenue_cents: int


class RevenueRowDay(BaseModel):
    """One local day's line of a revenue report (SPEC-6 Section 2.3).

    Attributes:
        date: The calendar day of the showtime in ``CINEMA_TIMEZONE``, not in
            UTC: a late-evening showtime belongs to its local day.
        tickets_sold: Confirmed seats of that day in the range.
        revenue_cents: ``tickets_sold`` times the showtimes' ``price_cents``,
            in integer cents.
    """

    date: datetime.date
    tickets_sold: int
    revenue_cents: int


class RevenueOut(BaseModel):
    """The revenue report (SPEC-6 Section 2.3, FR-6).

    ``total_cents`` and ``tickets_sold`` are the sums of ``rows``, computed from
    the same query result by :func:`src.reporting.rules.revenue_totals`, so the
    totals and the rows can never disagree. ``date_from`` and ``date_to`` echo
    the range that was applied, and are ``None`` when that side was unbounded.

    Attributes:
        date_from: First inclusive local day of the report, or ``None``.
        date_to: Last inclusive local day of the report, or ``None``.
        group_by: ``movie`` or ``day``; tells the client which row type to
            expect in ``rows``.
        total_cents: Sum of ``rows[*].revenue_cents``, in integer cents.
        tickets_sold: Sum of ``rows[*].tickets_sold``.
        rows: The grouped rows; empty when nothing was sold in the range.
    """

    date_from: datetime.date | None
    date_to: datetime.date | None
    group_by: GroupBy
    total_cents: int
    tickets_sold: int
    rows: list[RevenueRowMovie | RevenueRowDay]


class AdminReservationListParams(BaseModel):
    """The validated query of ``GET /admin/reservations`` (SPEC-6 Section 2.3).

    Every filter is optional; omitting one means "no filter", and an id that
    matches nothing yields an empty page with ``200``. Whether the two dates
    form a usable range is decided by
    :func:`src.reporting.rules.validate_date_range`, not here: a reversed range
    is a domain rule with its own ``INVALID_DATE_RANGE`` code, not a shape
    error.

    Attributes:
        movie_id: Keep reservations whose showtime shows this movie.
        showtime_id: Keep reservations of this showtime.
        date_from: First inclusive local day of the showtime, or ``None``.
        date_to: Last inclusive local day of the showtime, or ``None``.
        status: Keep reservations of this *effective* status, one of the four
            shared statuses; ``None`` means all of them.
        page: The validated pagination window reused from ``PageParams``:
            ``limit`` 1-100 (default 20) and ``offset`` 0 or greater.
    """

    model_config = ConfigDict(extra="forbid")

    movie_id: int | None = None
    showtime_id: int | None = None
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    status: ReservationStatus | None = None
    page: PageParams


class CapacityListParams(BaseModel):
    """The validated query of ``GET /admin/reports/capacity`` (Section 2.3).

    Attributes:
        showtime_id: Keep this showtime only.
        movie_id: Keep showtimes that show this movie.
        date_from: First inclusive local day of the showtime, or ``None``.
        date_to: Last inclusive local day of the showtime, or ``None``.
        page: The validated pagination window reused from ``PageParams``.
    """

    model_config = ConfigDict(extra="forbid")

    showtime_id: int | None = None
    movie_id: int | None = None
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    page: PageParams


class RevenueParams(BaseModel):
    """The validated query of ``GET /admin/reports/revenue`` (Section 2.3).

    This report is not paginated: it returns one row per movie or per day, and
    ``group_by=day`` already bounds the row count to 366.

    Attributes:
        date_from: First inclusive local day of the showtime, or ``None``.
        date_to: Last inclusive local day of the showtime, or ``None``.
        movie_id: Restrict the report to this movie.
        group_by: ``movie`` (the default) or ``day``; any other value is a
            validation error. ``day`` additionally requires both dates and at
            most 366 days, enforced by
            :func:`src.reporting.rules.validate_date_range`.
    """

    model_config = ConfigDict(extra="forbid")

    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    movie_id: int | None = None
    group_by: GroupBy = "movie"
