"""Aggregate report queries over an open database connection (SPEC-6).

This module answers the three admin reports of SPEC-6 Section 2.3 with SQL and
nothing else: it shapes the rows into the response models of
:mod:`src.reporting.schemas` and applies the pure rules of
:mod:`src.reporting.rules`. It builds no HTTP response and validates no query
string; the router owns both.

Properties every function here keeps:

* **Reads never write.** No function updates or deletes a row -- stale holds are
  not lazily expired, they are only *interpreted* through
  :func:`src.reservations.rules.hold_cutoff` (SPEC-4 Section 2.5, SPEC-6
  Section 2.5). No function commits, so a read can never end a transaction.
* **One instant per request.** ``now`` is passed in, never read from the clock,
  so the effective-status filter and the reported statuses of one response
  agree with each other.
* **One rule, one definition.** The date window, ``available_seats``, the revenue
  totals and the error code come from :mod:`src.reporting.rules`; the
  effective-status SQL condition comes from :mod:`src.db.reservation_filters`;
  the effective status and the cutoff come from
  :mod:`src.reservations.rules`. Nothing is recomputed here.

Import direction (SPEC-6 D7): ``reporting`` may import ``core``, ``db`` and the
two *pure* ``rules`` modules of ``reservations`` and ``showtimes``. It imports
no service, schema or router of another feature, because the reporting response
models are its own.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Join

from src.core.config import get_settings
from src.core.enums import DEFAULT_RESERVATION_STATUS, ReservationStatus
from src.core.errors import ApiError
from src.db.reservation_filters import effective_status_condition
from src.db.tables import (
    movies,
    reservations,
    screens,
    seat_reservations,
    seats,
    showtimes,
    users,
)
from src.reporting import rules as report_rules
from src.reporting.schemas import (
    AdminReservationItem,
    AdminReservationListParams,
    CapacityListParams,
    CapacityRow,
    MovieRef,
    ReportShowtimeRef,
    RevenueOut,
    RevenueParams,
    RevenueRowDay,
    RevenueRowMovie,
    ScreenRef,
    SeatRef,
    UserRef,
)
from src.reservations import rules as reservation_rules

logger = logging.getLogger(__name__)

#: The longest range ``group_by=day`` may cover, counted inclusively (FR-6).
#: SPEC-6 Section 4 adds no setting for it, so it is a constant here rather than
#: a number inlined at the call site.
MAX_GROUP_BY_DAY_DAYS = 366

#: The statuses the aggregates count, taken from the single vocabulary in
#: ``core/enums.py`` so no status string is spelled out in SQL.
_HELD_STATUS: ReservationStatus = DEFAULT_RESERVATION_STATUS
_CONFIRMED_STATUS: ReservationStatus = "confirmed"


def _invalid_date_range(exc: report_rules.InvalidDateRangeError) -> ApiError:
    """Translate a bad report range into the single ``INVALID_DATE_RANGE`` error.

    The rule lives in :mod:`src.reporting.rules`; this function is the one place
    that gives it its HTTP meaning (422, SPEC-6 Section 2.4), so the three
    reports cannot answer a reversed range differently. The message raised by
    the rule is kept, because it already names the offending dates.

    Args:
        exc: The rule violation raised by
            :func:`src.reporting.rules.validate_date_range`.

    Returns:
        ApiError: A 422 error carrying ``INVALID_DATE_RANGE``.
    """
    return ApiError(report_rules.INVALID_DATE_RANGE, str(exc), 422)


def _validate_range(
    date_from: date | None,
    date_to: date | None,
    *,
    require_both_max_days: int | None = None,
) -> None:
    """Check a report's dates, raising the API error instead of the rule error.

    Args:
        date_from: First inclusive local day, or ``None``.
        date_to: Last inclusive local day, or ``None``.
        require_both_max_days: Pass :data:`MAX_GROUP_BY_DAY_DAYS` for
            ``group_by=day`` and ``None`` for the reports whose dates are
            optional (see :func:`src.reporting.rules.validate_date_range`).

    Raises:
        ApiError: ``INVALID_DATE_RANGE`` with status 422 when the range is
            reversed, incomplete but required, or longer than the maximum.
    """
    try:
        report_rules.validate_date_range(
            date_from,
            date_to,
            require_both_max_days=require_both_max_days,
        )
    except report_rules.InvalidDateRangeError as exc:
        raise _invalid_date_range(exc) from exc


def _window_conditions(
    date_from: date | None,
    date_to: date | None,
    tz_name: str,
) -> list[ColumnElement[bool]]:
    """Build the ``showtimes.starts_at`` conditions of the date window (FR-3).

    The window is half-open, ``start <= starts_at < end``, where ``start`` is
    the local midnight beginning ``date_from`` and ``end`` the local midnight
    beginning the day after ``date_to``, both converted to UTC by
    :func:`src.reporting.rules.range_bounds_utc`. A missing date contributes no
    condition, so an unbounded report filters nothing.

    Call :func:`_validate_range` first: the conversion itself does not reject a
    reversed range (one rule has one definition).

    Args:
        date_from: First inclusive local day, or ``None``.
        date_to: Last inclusive local day, or ``None``.
        tz_name: IANA name of the cinema's timezone (``CINEMA_TIMEZONE``).

    Returns:
        list[ColumnElement[bool]]: Zero, one or two SQL conditions, ready to be
        passed to ``where``.
    """
    start, end = report_rules.range_bounds_utc(date_from, date_to, tz_name)
    conditions: list[ColumnElement[bool]] = []
    if start is not None:
        conditions.append(showtimes.c.starts_at >= start)
    if end is not None:
        conditions.append(showtimes.c.starts_at < end)
    return conditions


def _admin_reservations_from() -> Join:
    """Return the join every admin-reservation query reads from.

    The page query and the count query share it, so their filters select from
    the same rows: an inner join on the foreign keys of ``reservations`` cannot
    drop a reservation, because ``user_id`` and ``showtime_id`` are non-null and
    every referenced row exists.

    Returns:
        Join: ``reservations`` joined to ``users``, ``showtimes``, ``movies``
        and ``screens``.
    """
    return (
        reservations.join(users, users.c.id == reservations.c.user_id)
        .join(showtimes, showtimes.c.id == reservations.c.showtime_id)
        .join(movies, movies.c.id == showtimes.c.movie_id)
        .join(screens, screens.c.id == showtimes.c.screen_id)
    )


def _showtime_ref(row: Any) -> ReportShowtimeRef:
    """Shape a selected row's showtime columns into the response model.

    Both the admin list and the capacity report embed the same showtime shape,
    so both build it here instead of repeating the nesting.

    Args:
        row: A result row that selected ``showtime_id``, ``starts_at``,
            ``price_cents``, ``movie_id``, ``title``, ``screen_id`` and
            ``screen_name``.

    Returns:
        ReportShowtimeRef: The showtime, its movie and its screen.
    """
    return ReportShowtimeRef(
        id=row.showtime_id,
        starts_at=row.starts_at,
        price_cents=row.price_cents,
        movie=MovieRef(id=row.movie_id, title=row.title),
        screen=ScreenRef(id=row.screen_id, name=row.screen_name),
    )


def _applied_filter_names(filters: BaseModel) -> list[str]:
    """Return the *names* of the filters a report request applied (FR-7).

    Only names are returned, never values: an id or a date in a log line is
    request data, and a name is all FR-7 asks for. ``page`` is left out because
    ``limit`` and ``offset`` are passed to the service explicitly. The list is
    sorted, so one request always produces the same log text.

    Args:
        filters: One of the validated query models of
            :mod:`src.reporting.schemas`.

    Returns:
        list[str]: Sorted names of the fields whose value is set. ``group_by``
        is always present, because it always has a default.
    """
    return sorted(filters.model_dump(exclude={"page"}, exclude_none=True))


def _log_report_request(
    event: str,
    admin_user_id: int,
    filters: BaseModel,
) -> None:
    """Write the one INFO line every report request produces (FR-7).

    The line names the event, the admin who asked and the filters that were
    applied -- never an email, a token, a returned row or a request body
    (SPEC-6 Section 7).

    Args:
        event: The event name, e.g. ``admin_capacity_report``.
        admin_user_id: Primary key of the authenticated admin.
        filters: The validated query model whose used filter names are logged.

    Returns:
        None: The function is called for its side effect.
    """
    logger.info(
        "%s admin_user_id=%s filters=%s",
        event,
        admin_user_id,
        ",".join(_applied_filter_names(filters)) or "-",
    )


async def list_reservations_admin(
    conn: AsyncConnection,
    filters: AdminReservationListParams,
    limit: int,
    offset: int,
    now: datetime,
    *,
    admin_user_id: int,
) -> tuple[list[AdminReservationItem], int]:
    """Return one page of every user's reservations, newest first (FR-5).

    One query reads the page -- reservations joined to their owner, showtime,
    movie and screen -- one more reads the seats of exactly those reservations,
    and a third counts every reservation that matches the same filters,
    ignoring ``limit``/``offset``. The page therefore costs three round trips
    whatever its size: there is no per-reservation seat query (SPEC-6
    Section 2.5).

    The status filter and the reported status use the same two rules as the
    SPEC-5 list: :func:`src.db.reservation_filters.effective_status_condition`
    in SQL and :func:`src.reservations.rules.effective_status` in Python, so a
    stale ``held`` row is *reported* as ``expired`` and selected by the
    ``expired`` filter while the stored row is left untouched (reads never
    write, SPEC-6 Section 2.5).

    Unknown ``movie_id`` / ``showtime_id`` values are not an error: they simply
    match nothing and the page comes back empty with a ``total`` of zero.

    Args:
        conn: An open connection. This function only reads and never commits.
        filters: The validated query: optional ``movie_id``, ``showtime_id``,
            ``date_from``/``date_to`` (inclusive local days of the showtime)
            and effective ``status``. Its ``page`` field is not read here --
            the window arrives as ``limit`` and ``offset``.
        limit: Maximum number of rows to return (1-100, validated by the
            router).
        offset: Number of rows to skip before the page starts (``>= 0``).
        now: The current instant, timezone-aware. One value per request, so the
            status filter and the reported statuses agree.
        admin_user_id: Primary key of the authenticated admin; logged for FR-7
            and used for nothing else.

    Returns:
        tuple[list[AdminReservationItem], int]: The page's items in
        ``created_at DESC, id DESC`` order with their seats ordered by row
        label then seat number, and the total number of matching reservations
        before pagination.

    Raises:
        ApiError: ``INVALID_DATE_RANGE`` with status 422 when ``date_from`` is
            after ``date_to`` (FR-3, FR-12).
        ValueError: If ``now`` is naive; raised by
            :func:`src.reservations.rules.hold_cutoff`.
    """
    settings = get_settings()
    hold_minutes = settings.hold_minutes
    # One cutoff for the filter and for the reported statuses, so they cannot
    # disagree about where a hold expires (SPEC-5 Section 2.5, DRY check).
    cutoff = reservation_rules.hold_cutoff(now, hold_minutes)

    _validate_range(filters.date_from, filters.date_to)
    _log_report_request("admin_reservation_list", admin_user_id, filters)

    conditions: list[ColumnElement[bool]] = _window_conditions(
        filters.date_from, filters.date_to, settings.cinema_timezone
    )
    if filters.movie_id is not None:
        conditions.append(showtimes.c.movie_id == filters.movie_id)
    if filters.showtime_id is not None:
        conditions.append(reservations.c.showtime_id == filters.showtime_id)
    if filters.status is not None:
        conditions.append(effective_status_condition(filters.status, cutoff))

    from_clause = _admin_reservations_from()
    page_rows = (
        await conn.execute(
            select(
                reservations.c.id,
                reservations.c.status,
                reservations.c.created_at,
                reservations.c.confirmed_at,
                reservations.c.cancelled_at,
                showtimes.c.id.label("showtime_id"),
                showtimes.c.starts_at,
                showtimes.c.price_cents,
                movies.c.id.label("movie_id"),
                movies.c.title,
                screens.c.id.label("screen_id"),
                screens.c.name.label("screen_name"),
                users.c.id.label("user_id"),
                users.c.email,
            )
            .select_from(from_clause)
            .where(*conditions)
            .order_by(reservations.c.created_at.desc(), reservations.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    # One extra query for the whole page's seats, grouped in Python. The ids
    # come from the page itself, so an empty page skips the query entirely
    # instead of asking for ``IN ()``.
    reservation_ids = [row.id for row in page_rows]
    seats_by_reservation: dict[int, list[SeatRef]] = {}
    if reservation_ids:
        seat_rows = (
            await conn.execute(
                select(
                    seat_reservations.c.reservation_id,
                    seats.c.id,
                    seats.c.row_label,
                    seats.c.seat_number,
                )
                .select_from(
                    seat_reservations.join(
                        seats, seats.c.id == seat_reservations.c.seat_id
                    )
                )
                .where(seat_reservations.c.reservation_id.in_(reservation_ids))
                .order_by(
                    seat_reservations.c.reservation_id,
                    seats.c.row_label,
                    seats.c.seat_number,
                )
            )
        ).all()
        for seat_row in seat_rows:
            seats_by_reservation.setdefault(seat_row.reservation_id, []).append(
                SeatRef(
                    seat_id=seat_row.id,
                    row_label=seat_row.row_label,
                    seat_number=seat_row.seat_number,
                )
            )

    # The count reuses the same conditions and the same FROM, so the total
    # describes exactly the rows the page query filtered.
    total = (
        await conn.execute(
            select(func.count()).select_from(from_clause).where(*conditions)
        )
    ).scalar_one()

    items: list[AdminReservationItem] = []
    for row in page_rows:
        # The single effective-status rule (SPEC-5 FR-3); nothing is written.
        effective = reservation_rules.effective_status(
            row.status,
            row.created_at,
            now,
            hold_minutes,
        )
        reservation_seats = seats_by_reservation.get(row.id, [])
        # ``expires_at`` is meaningful only while the reservation is still
        # effectively held (SPEC-4 Section 2.3).
        expires_at: datetime | None = (
            reservation_rules.hold_expires_at(row.created_at, hold_minutes)
            if effective == _HELD_STATUS
            else None
        )
        items.append(
            AdminReservationItem(
                id=row.id,
                status=effective,
                created_at=row.created_at,
                expires_at=expires_at,
                confirmed_at=row.confirmed_at,
                cancelled_at=row.cancelled_at,
                seats=reservation_seats,
                # Money is integer cents: the price times the seats this
                # reservation covers, never a float and never a stored total.
                total_cents=row.price_cents * len(reservation_seats),
                user=UserRef(id=row.user_id, email=row.email),
                showtime=_showtime_ref(row),
            )
        )

    return items, total


async def capacity_report(
    conn: AsyncConnection,
    filters: CapacityListParams,
    limit: int,
    offset: int,
    now: datetime,
    *,
    admin_user_id: int,
) -> tuple[list[CapacityRow], int]:
    """Return one page of per-showtime capacity (FR-4).

    One query over ``showtimes`` joined to ``movies`` and ``screens`` computes
    the three counts as correlated aggregates, so a showtime contributes one row
    whether it has zero, one or many reservation rows:

    * ``total_seats`` -- seats of the showtime's screen;
    * ``sold_seats`` -- seat rows of the showtime with status ``confirmed``;
    * ``held_seats`` -- seat rows of the showtime with status ``held`` whose
      reservation was created after the cutoff, i.e. holds that have not
      expired. Expired holds are excluded by the comparison, not fixed up by a
      write (SPEC-6 Section 2.5).

    ``available_seats`` is then computed by
    :func:`src.reporting.rules.available_seats`, so the subtraction has one
    definition and can never go below zero. A showtime with no reservations at
    all is still listed, with all its seats available (FR-4). A second query
    counts the showtimes matching the same filters, ignoring
    ``limit``/``offset``.

    Args:
        conn: An open connection. This function only reads and never commits.
        filters: The validated query: optional ``showtime_id``, ``movie_id``
            and ``date_from``/``date_to`` (inclusive local days of the
            showtime). Its ``page`` field is not read here -- the window
            arrives as ``limit`` and ``offset``.
        limit: Maximum number of rows to return (1-100, validated by the
            router).
        offset: Number of rows to skip before the page starts (``>= 0``).
        now: The current instant, timezone-aware, from which the hold cutoff is
            derived. One value per request, so every row of a response agrees.
        admin_user_id: Primary key of the authenticated admin; logged for FR-7
            and used for nothing else.

    Returns:
        tuple[list[CapacityRow], int]: The page's rows in ``starts_at, id``
        order and the total number of matching showtimes before pagination.

    Raises:
        ApiError: ``INVALID_DATE_RANGE`` with status 422 when ``date_from`` is
            after ``date_to`` (FR-3, FR-12).
        ValueError: If ``now`` is naive; raised by
            :func:`src.reservations.rules.hold_cutoff`.
    """
    settings = get_settings()
    cutoff = reservation_rules.hold_cutoff(now, settings.hold_minutes)

    _validate_range(filters.date_from, filters.date_to)
    _log_report_request("admin_capacity_report", admin_user_id, filters)

    conditions: list[ColumnElement[bool]] = _window_conditions(
        filters.date_from, filters.date_to, settings.cinema_timezone
    )
    if filters.movie_id is not None:
        conditions.append(showtimes.c.movie_id == filters.movie_id)
    if filters.showtime_id is not None:
        conditions.append(showtimes.c.id == filters.showtime_id)

    # Each aggregate is correlated to the showtime of the enclosing row, so no
    # join can multiply the showtime away and no showtime needs a second query.
    total_seats = (
        select(func.count())
        .select_from(seats)
        .where(seats.c.screen_id == showtimes.c.screen_id)
        .scalar_subquery()
    )
    sold_seats = (
        select(func.count())
        .select_from(seat_reservations)
        .where(
            seat_reservations.c.showtime_id == showtimes.c.id,
            seat_reservations.c.status == _CONFIRMED_STATUS,
        )
        .scalar_subquery()
    )
    held_seats = (
        select(func.count())
        .select_from(
            seat_reservations.join(
                reservations, reservations.c.id == seat_reservations.c.reservation_id
            )
        )
        .where(
            seat_reservations.c.showtime_id == showtimes.c.id,
            seat_reservations.c.status == _HELD_STATUS,
            reservations.c.created_at > cutoff,
        )
        .scalar_subquery()
    )

    from_clause = showtimes.join(movies, movies.c.id == showtimes.c.movie_id).join(
        screens, screens.c.id == showtimes.c.screen_id
    )
    page_rows = (
        await conn.execute(
            select(
                showtimes.c.id.label("showtime_id"),
                showtimes.c.starts_at,
                showtimes.c.price_cents,
                movies.c.id.label("movie_id"),
                movies.c.title,
                screens.c.id.label("screen_id"),
                screens.c.name.label("screen_name"),
                total_seats.label("total_seats"),
                sold_seats.label("sold_seats"),
                held_seats.label("held_seats"),
            )
            .select_from(from_clause)
            .where(*conditions)
            .order_by(showtimes.c.starts_at, showtimes.c.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()

    # Same conditions, same FROM: the total counts exactly the showtimes the
    # page query selected.
    total = (
        await conn.execute(
            select(func.count()).select_from(from_clause).where(*conditions)
        )
    ).scalar_one()

    rows: list[CapacityRow] = []
    for row in page_rows:
        # ``COUNT`` comes back as an integer of the driver's choosing, so the
        # values are converted here before the pure rule sees them.
        total_row_seats = int(row.total_seats)
        sold_row_seats = int(row.sold_seats)
        held_row_seats = int(row.held_seats)
        rows.append(
            CapacityRow(
                showtime=_showtime_ref(row),
                total_seats=total_row_seats,
                sold_seats=sold_row_seats,
                held_seats=held_row_seats,
                available_seats=report_rules.available_seats(
                    total_row_seats,
                    sold_row_seats,
                    held_row_seats,
                ),
            )
        )

    return rows, total


async def revenue_report(
    conn: AsyncConnection,
    filters: RevenueParams,
    *,
    admin_user_id: int,
) -> RevenueOut:
    """Return revenue grouped by movie or by local day (FR-6).

    One aggregate query does the whole report: confirmed seat rows joined to
    their showtime (and to the movie, for the title), filtered by the date
    window and the optional ``movie_id``, grouped by the movie's id or by the
    showtime's local date. Each row counts tickets as ``COUNT(*)`` and money as
    ``SUM(showtimes.price_cents)`` -- one ticket costs exactly one showtime
    price, so no per-seat price column is needed -- and the sum is converted to
    a Python ``int`` (money is integer cents, never a float).

    ``total_cents`` and ``tickets_sold`` are then summed from those very rows by
    :func:`src.reporting.rules.revenue_totals`, so the totals can never disagree
    with the rows they describe. Because it is one statement, it is one
    snapshot: the report stays internally consistent while confirmations happen
    concurrently (SPEC-6 Section 2.5).

    Only groups with at least one sold ticket appear; a range with no confirmed
    seats is a ``200`` with zero totals and no rows, not an error. The report is
    not paginated: ``group_by=day`` is bounded by
    :data:`MAX_GROUP_BY_DAY_DAYS` and ``group_by=movie`` by the number of movies.

    Args:
        conn: An open connection. This function only reads and never commits.
        filters: The validated query: optional ``date_from``/``date_to``
            (inclusive local days of the showtime), optional ``movie_id`` and
            the ``group_by`` mode.
        admin_user_id: Primary key of the authenticated admin; logged for FR-7
            and used for nothing else.

    Returns:
        RevenueOut: The echoed range and grouping, the rows in
        ``revenue_cents DESC, movie_id`` order (``movie``) or ``date`` order
        (``day``), and their totals.

    Raises:
        ApiError: ``INVALID_DATE_RANGE`` with status 422 when the range is
            reversed, when ``group_by=day`` lacks either date, or when
            ``group_by=day`` spans more than :data:`MAX_GROUP_BY_DAY_DAYS`
            inclusive days (FR-6, FR-12).
    """
    settings = get_settings()
    _validate_range(
        filters.date_from,
        filters.date_to,
        require_both_max_days=(
            MAX_GROUP_BY_DAY_DAYS if filters.group_by == "day" else None
        ),
    )
    _log_report_request("admin_revenue_report", admin_user_id, filters)

    conditions: list[ColumnElement[bool]] = [
        seat_reservations.c.status == _CONFIRMED_STATUS,
        *_window_conditions(
            filters.date_from, filters.date_to, settings.cinema_timezone
        ),
    ]
    if filters.movie_id is not None:
        conditions.append(showtimes.c.movie_id == filters.movie_id)

    # One ticket costs exactly one showtime's price (there is no per-seat
    # price), so a group's money is the sum of its showtimes' prices.
    revenue = func.sum(showtimes.c.price_cents)

    # One list for both groupings: the row union is what ``RevenueOut`` holds,
    # and the same list feeds the totals below.
    report_rows: list[RevenueRowMovie | RevenueRowDay]
    if filters.group_by == "movie":
        result_rows = (
            await conn.execute(
                select(
                    movies.c.id.label("movie_id"),
                    movies.c.title,
                    func.count().label("tickets_sold"),
                    revenue.label("revenue_cents"),
                )
                .select_from(
                    seat_reservations.join(
                        showtimes, showtimes.c.id == seat_reservations.c.showtime_id
                    ).join(movies, movies.c.id == showtimes.c.movie_id)
                )
                .where(*conditions)
                .group_by(movies.c.id, movies.c.title)
                .order_by(revenue.desc(), movies.c.id)
            )
        ).all()
        report_rows = [
            RevenueRowMovie(
                movie_id=row.movie_id,
                title=row.title,
                tickets_sold=int(row.tickets_sold),
                revenue_cents=int(row.revenue_cents),
            )
            for row in result_rows
        ]
    else:
        # ``timezone(CINEMA_TIMEZONE, starts_at)::date``: a showtime at 23:30
        # local belongs to its local day, not to the UTC day (FR-6).
        local_day = cast(
            func.timezone(settings.cinema_timezone, showtimes.c.starts_at), Date
        )
        result_rows = (
            await conn.execute(
                select(
                    local_day.label("day"),
                    func.count().label("tickets_sold"),
                    revenue.label("revenue_cents"),
                )
                .select_from(
                    seat_reservations.join(
                        showtimes, showtimes.c.id == seat_reservations.c.showtime_id
                    )
                )
                .where(*conditions)
                .group_by(local_day)
                .order_by(local_day)
            )
        ).all()
        report_rows = [
            RevenueRowDay(
                date=row.day,
                tickets_sold=int(row.tickets_sold),
                revenue_cents=int(row.revenue_cents),
            )
            for row in result_rows
        ]

    total_cents, tickets_sold = report_rules.revenue_totals(report_rows)
    return RevenueOut(
        date_from=filters.date_from,
        date_to=filters.date_to,
        group_by=filters.group_by,
        total_cents=total_cents,
        tickets_sold=tickets_sold,
        rows=report_rows,
    )
