"""Screen and showtime business rules over an open database connection.

This module owns the screen and showtime rules from SPEC-3 Sections 2.5 and 2.7
(FR-5 for screen and showtime ids, FR-9 through FR-15). It is deliberately free
of HTTP and FastAPI imports: every function takes an already-open
:class:`~sqlalchemy.ext.asyncio.AsyncConnection`, never opens one itself and
never commits, so the request-scoped transaction stays owned by
:func:`src.db.engine.get_connection` and the same functions can be driven
straight from a test connection.

Import direction (SPEC-3 Section 11.1): ``showtimes`` may import ``core`` and
``db``. It does **not** import ``movies`` -- the movie rows a showtime embeds are
read from the ``movies`` table through :mod:`src.db.tables` directly, and the
movie-not-found error is built here. Within the module, ``service`` imports
:mod:`src.showtimes.schemas` and :mod:`src.showtimes.rules`; neither imports
``service``, so there is no cycle.

Timing and settings: :func:`create_showtime`, :func:`list_showtimes` and
:func:`delete_showtime` take the current instant ``now`` as a parameter instead
of reading the clock, so a test can pin the boundary (SPEC-3 test 31: ``now +
1 s`` accepted, ``now`` and ``now - 1 s`` rejected). ``SHOWTIME_SLOT_MINUTES``
and ``CINEMA_TIMEZONE`` come from settings, the one place they exist (SPEC-3
Section 10, DRY check); the arithmetic they feed lives in
:mod:`src.showtimes.rules`.

Query shape notes:

* ``list_showtimes`` embeds the movie and the screen through ONE JOIN, not one
  query per row (SPEC-3 Section 2.5, "Browse showtimes"), and counts with a
  filter-only query that needs no join at all, so ``idx_showtimes_date`` /
  ``idx_showtimes_movie_date`` are usable.
* Every returned value is a small frozen dataclass, never a live SQLAlchemy
  :class:`~sqlalchemy.engine.Row`, so a row cannot be read after its transaction
  has ended.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, Row, delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from src.core.config import get_settings
from src.core.enums import DEFAULT_SEAT_TYPE
from src.core.errors import ApiError
from src.db.tables import movies, reservations, screens, seats, showtimes
from src.showtimes import rules
from src.showtimes.schemas import ScreenCreate, ShowtimeCreate

logger = logging.getLogger(__name__)

# The SPEC-3 Section 2.7 codes that belong to screens and showtimes, declared
# once here so no bare code string is scattered through the module (Section 10,
# DRY check). ``MOVIE_NOT_FOUND`` is repeated rather than imported from
# ``movies``: the two modules must not import each other (Section 11.1) and an
# error code is a module-local constant.
MOVIE_NOT_FOUND = "MOVIE_NOT_FOUND"
SCREEN_NOT_FOUND = "SCREEN_NOT_FOUND"
SCREEN_NAME_TAKEN = "SCREEN_NAME_TAKEN"
SHOWTIME_NOT_FOUND = "SHOWTIME_NOT_FOUND"
SHOWTIME_IN_PAST = "SHOWTIME_IN_PAST"
SHOWTIME_OVERLAP = "SHOWTIME_OVERLAP"
SHOWTIME_STARTED = "SHOWTIME_STARTED"
SHOWTIME_HAS_RESERVATIONS = "SHOWTIME_HAS_RESERVATIONS"

# Seat row labels are generated upward from ``A``; ``ScreenCreate`` bounds
# ``rows`` to 26, so the last label is ``Z`` (SPEC-3 Section 2.4, FR-9).
_FIRST_ROW_LETTER = ord("A")


def _movie_not_found() -> ApiError:
    """Build the 404 returned when a showtime names an unknown movie.

    Returns:
        ApiError: ``MOVIE_NOT_FOUND`` with status 404 (SPEC-3 FR-5, decision
        D6; test 32).
    """
    return ApiError(MOVIE_NOT_FOUND, "No movie with that id exists.", 404)


def _screen_not_found() -> ApiError:
    """Build the 404 returned when a showtime names an unknown screen.

    Returns:
        ApiError: ``SCREEN_NOT_FOUND`` with status 404 (SPEC-3 FR-5, decision
        D6; test 32).
    """
    return ApiError(SCREEN_NOT_FOUND, "No screen with that id exists.", 404)


def _screen_name_taken() -> ApiError:
    """Build the 409 returned when a screen name is already in use.

    Returns:
        ApiError: ``SCREEN_NAME_TAKEN`` with status 409 (SPEC-3 FR-9).
    """
    return ApiError(
        SCREEN_NAME_TAKEN,
        "A screen with that name already exists.",
        409,
    )


def _showtime_not_found() -> ApiError:
    """Build the 404 returned when a showtime id does not exist.

    Returns:
        ApiError: ``SHOWTIME_NOT_FOUND`` with status 404 (SPEC-3 Section 2.7).
    """
    return ApiError(SHOWTIME_NOT_FOUND, "No showtime with that id exists.", 404)


def _showtime_in_past() -> ApiError:
    """Build the 422 returned when a new showtime would start in the past.

    Returns:
        ApiError: ``SHOWTIME_IN_PAST`` with status 422 (SPEC-3 FR-10). The
        status is 422 rather than 409 because the start instant is a value
        problem, not a conflict with another resource.
    """
    return ApiError(
        SHOWTIME_IN_PAST,
        "A showtime must start in the future.",
        422,
    )


def _showtime_overlap() -> ApiError:
    """Build the 409 returned when a showtime collides with another one.

    Returns:
        ApiError: ``SHOWTIME_OVERLAP`` with status 409 (SPEC-3 FR-11).
    """
    return ApiError(
        SHOWTIME_OVERLAP,
        "Another showtime on this screen is too close to that start time.",
        409,
    )


def _showtime_started() -> ApiError:
    """Build the 409 returned when deleting a showtime that already started.

    Returns:
        ApiError: ``SHOWTIME_STARTED`` with status 409 (SPEC-3 FR-12, decision
        D4).
    """
    return ApiError(
        SHOWTIME_STARTED,
        "This showtime has already started and cannot be deleted.",
        409,
    )


def _showtime_has_reservations() -> ApiError:
    """Build the 409 returned when deleting a showtime that has reservations.

    Any reservation counts, whatever its status, because history is kept
    (SPEC-3 FR-12).

    Returns:
        ApiError: ``SHOWTIME_HAS_RESERVATIONS`` with status 409.
    """
    return ApiError(
        SHOWTIME_HAS_RESERVATIONS,
        "This showtime has reservations and cannot be deleted.",
        409,
    )


@dataclass(frozen=True, slots=True)
class ScreenRecord:
    """A screen as the service returns it.

    The shape mirrors SPEC-3's ``ScreenOut`` so the route layer can hand this
    straight to FastAPI's response model. ``seat_count`` is derived here, once,
    from the two stored dimensions.

    Attributes:
        id: The screen's primary key.
        name: The stored name.
        rows: Number of seat rows.
        seats_per_row: Seats in each row.
        seat_count: Total number of seats, ``rows * seats_per_row``.
    """

    id: int
    name: str
    rows: int
    seats_per_row: int
    seat_count: int


@dataclass(frozen=True, slots=True)
class ShowtimeMovieRecord:
    """The movie identity a :class:`ShowtimeRecord` embeds.

    Attributes:
        id: The movie's primary key.
        title: The stored title.
        poster_url: The stored poster URL, or ``None`` when there is no poster.
    """

    id: int
    title: str
    poster_url: str | None


@dataclass(frozen=True, slots=True)
class ShowtimeScreenRecord:
    """The screen identity a :class:`ShowtimeRecord` embeds.

    Attributes:
        id: The screen's primary key.
        name: The stored name.
    """

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class ShowtimeRecord:
    """A showtime with its movie and screen, as the service returns it.

    The public shape mirrors SPEC-3's ``ShowtimeOut``. The nested movie and
    screen come from the same JOIN that produced the row, never from a second
    query.

    Attributes:
        id: The showtime's primary key.
        starts_at: The start instant, in UTC.
        price_cents: Ticket price in integer cents.
        movie: The scheduled movie's identity and poster.
        screen: The screen's identity.
    """

    id: int
    starts_at: datetime
    price_cents: int
    movie: ShowtimeMovieRecord
    screen: ShowtimeScreenRecord


# The ``screens`` columns every read and the insert ``RETURNING`` clause share,
# so the selected shape cannot drift from the one ``ScreenRecord`` maps.
_SCREEN_COLUMNS = (
    screens.c.id,
    screens.c.name,
    screens.c.rows,
    screens.c.seats_per_row,
)

# The showtime row plus the movie and screen fields the response embeds, all
# fetched by ONE query (SPEC-3 Section 2.5, "Browse showtimes"). The movie and
# screen columns are selected as labels because the three tables each have an
# ``id`` and showtimes/screens both have a ``name``-like column, so unlabelled
# selection would make the row ambiguous.
_SHOWTIME_COLUMNS = (
    showtimes.c.id,
    showtimes.c.starts_at,
    showtimes.c.price_cents,
    movies.c.id.label("movie_id"),
    movies.c.title.label("movie_title"),
    movies.c.poster_url.label("movie_poster_url"),
    screens.c.id.label("screen_id"),
    screens.c.name.label("screen_name"),
)


def _seat_rows(
    screen_id: int,
    rows: int,
    seats_per_row: int,
) -> list[dict[str, int | str]]:
    """Build the insert parameters for a screen's generated seats (FR-9).

    Pure: it reads no connection and no clock, so the label scheme -- row letters
    from ``A`` upward, seat numbers from ``1`` upward, every seat of type
    ``standard`` -- is directly testable (SPEC-3 test 26). The result is the
    parameter list for one bulk insert, so a full screen is written in a single
    statement rather than ``rows * seats_per_row`` statements.

    Args:
        screen_id: The screen the seats belong to.
        rows: Number of rows; :class:`~src.showtimes.schemas.ScreenCreate`
            bounds it to 26 so the letters stop at ``Z``.
        seats_per_row: Seats in each row; the schema bounds it so the numbers
            fit the ``smallint`` column.

    Returns:
        list[dict[str, int | str]]: One mapping per seat, in row-major order
        (row ``A`` seat ``1`` first). The first dictionary is the one with the
        lowest row label and seat number, which keeps the insert -- and any test
        that inspects these rows -- deterministic.
    """
    return [
        {
            "screen_id": screen_id,
            "row_label": chr(_FIRST_ROW_LETTER + row_index),
            "seat_number": seat_number,
            "seat_type": DEFAULT_SEAT_TYPE,
        }
        for row_index in range(rows)
        for seat_number in range(1, seats_per_row + 1)
    ]


def _screen_record(row: Row[*tuple[Any, ...]]) -> ScreenRecord:
    """Map a selected ``screens`` row to a :class:`ScreenRecord`.

    Args:
        row: A row holding the columns in :data:`_SCREEN_COLUMNS`.

    Returns:
        ScreenRecord: The row as the public shape, with ``seat_count`` derived
        from the row's own dimensions.
    """
    return ScreenRecord(
        id=row.id,
        name=row.name,
        rows=row.rows,
        seats_per_row=row.seats_per_row,
        seat_count=row.rows * row.seats_per_row,
    )


def _showtime_record(row: Row[*tuple[Any, ...]]) -> ShowtimeRecord:
    """Map a joined showtime/movie/screen row to a :class:`ShowtimeRecord`.

    Args:
        row: A row holding the columns in :data:`_SHOWTIME_COLUMNS`.

    Returns:
        ShowtimeRecord: The row as the public shape, with the movie and screen
        nested from the same row rather than from extra queries.
    """
    return ShowtimeRecord(
        id=row.id,
        starts_at=row.starts_at,
        price_cents=row.price_cents,
        movie=ShowtimeMovieRecord(
            id=row.movie_id,
            title=row.movie_title,
            poster_url=row.movie_poster_url,
        ),
        screen=ShowtimeScreenRecord(id=row.screen_id, name=row.screen_name),
    )


def _showtime_filters(
    day: date | None,
    movie_id: int | None,
    now: datetime,
) -> list[ColumnElement[bool]]:
    """Build the ``WHERE`` conditions that select a page of showtimes (FR-13).

    ``day`` and ``now`` are mutually exclusive in effect: when ``day`` is given
    the window replaces the "not yet started" rule, because browsing a past date
    is legitimate (SPEC-3 Section 2.5, "Browse showtimes"). The window is
    ``[local midnight, next local midnight)`` in ``CINEMA_TIMEZONE`` converted to
    UTC, so a showtime at 23:30 local appears under its local date and the bounds
    stay correct on 23- and 25-hour DST days.

    Args:
        day: The calendar day to browse, or ``None`` for "everything from
            ``now`` onward".
        movie_id: When given, only showtimes of this movie are matched. An
            unknown id is not an error: it simply matches nothing (SPEC-3
            Section 2.5).
        now: The current instant, timezone-aware UTC. Only used when ``day`` is
            ``None``; a naive value would make the comparison depend on the
            server's local timezone.

    Returns:
        list[ColumnElement[bool]]: The conditions, ready to be unpacked into
        ``select(...).where(*conditions)``. Both the page query and the count
        query use the same list, so the two can never disagree.
    """
    conditions: list[ColumnElement[bool]] = []
    if day is None:
        conditions.append(showtimes.c.starts_at >= now)
    else:
        start_utc, end_utc = rules.day_bounds_utc(day, get_settings().cinema_timezone)
        conditions.append(showtimes.c.starts_at >= start_utc)
        conditions.append(showtimes.c.starts_at < end_utc)
    if movie_id is not None:
        conditions.append(showtimes.c.movie_id == movie_id)
    return conditions


async def list_screens(conn: AsyncConnection) -> list[ScreenRecord]:
    """List every screen, ordered by name (SPEC-3 Section 2.3).

    Ordering is by the lowercased name and then by id, so the list reads the
    same regardless of the case a name was stored with and ties are
    deterministic.

    Args:
        conn: An open connection. This function only reads.

    Returns:
        list[ScreenRecord]: All screens, each with its ``seat_count`` derived
        from its dimensions. An empty list when no screen exists.
    """
    rows = (
        await conn.execute(
            select(*_SCREEN_COLUMNS).order_by(func.lower(screens.c.name), screens.c.id)
        )
    ).all()
    return [_screen_record(row) for row in rows]


async def create_screen(
    conn: AsyncConnection,
    data: ScreenCreate,
    actor_id: int,
) -> ScreenRecord:
    """Create a screen together with its generated seats (SPEC-3 FR-9).

    The screen insert runs inside a SAVEPOINT (``begin_nested``) so an
    ``IntegrityError`` from the UNIQUE constraint on ``screens.name`` is mapped
    to a clean 409 ``SCREEN_NAME_TAKEN`` without poisoning the surrounding
    request transaction; because the failure happens before the seats are built,
    no orphan seat can be left behind. The seats are then written in ONE bulk
    insert (row labels ``A``.., seat numbers ``1``.., all ``standard``), so the
    screen and all ``rows * seats_per_row`` seats exist together or not at all
    (SPEC-3 Section 2.5, "Screen create"; test 27).

    No pre-check is made for the name: the SPEC prescribes the savepoint plus
    ``IntegrityError`` mapping, and the UNIQUE constraint is the single source of
    truth for the rule (``principles.md``: the database enforces uniqueness).

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        data: The validated request body. ``rows`` and ``seats_per_row`` are
            already bounded, and ``name`` is already stripped.
        actor_id: The id of the signed-in admin performing the write. Used only
            for the audit log line and never written to the row.

    Returns:
        ScreenRecord: The created screen, with ``seat_count`` derived from its
        dimensions.

    Raises:
        ApiError: ``SCREEN_NAME_TAKEN`` with status 409 when a screen already
            has that name.
    """
    try:
        async with conn.begin_nested():
            row = (
                await conn.execute(
                    insert(screens)
                    .values(
                        name=data.name,
                        rows=data.rows,
                        seats_per_row=data.seats_per_row,
                    )
                    .returning(*_SCREEN_COLUMNS)
                )
            ).one()
    except IntegrityError as error:
        # The only uniqueness rule on ``screens`` is the name, so any integrity
        # failure here is a duplicate name.
        logger.info("screen_create_rejected reason=name_taken")
        raise _screen_name_taken() from error

    await conn.execute(
        insert(seats),
        _seat_rows(row.id, data.rows, data.seats_per_row),
    )

    logger.info("screen_created actor=%s screen_id=%s", actor_id, row.id)
    return _screen_record(row)


async def create_showtime(
    conn: AsyncConnection,
    data: ShowtimeCreate,
    actor_id: int,
    now: datetime,
) -> ShowtimeRecord:
    """Create a showtime after enforcing the scheduling rules (SPEC-3 2.5).

    The steps follow SPEC-3 Section 2.5 exactly, and their order matters: the
    movie and screen are resolved first (404 ``MOVIE_NOT_FOUND`` /
    ``SCREEN_NOT_FOUND``, decision D6), then the start instant is compared with
    ``now`` (422 ``SHOWTIME_IN_PAST``), then the screen row is locked with
    ``SELECT ... FOR UPDATE`` so two concurrent creates on the same screen are
    serialized, and only then is the overlap check run (409
    ``SHOWTIME_OVERLAP``). Without the lock the check-then-insert pair would race,
    because there is no database constraint expressing the slot rule
    (SPEC-3 FR-11, test 33).

    The overlap window is ``rules.conflict_window`` with
    ``SHOWTIME_SLOT_MINUTES`` and both bounds exclusive, so a showtime exactly one
    slot before or after is allowed and one minute closer is refused.

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        data: The validated request body. ``starts_at`` is already
            timezone-aware UTC and ``price_cents`` is already a strict integer in
            range.
        actor_id: The id of the signed-in admin performing the write. Used only
            for the audit log line and never written to the row.
        now: The current instant, timezone-aware UTC, passed in rather than read
            from the clock so a test can pin the boundary (SPEC-3 test 31). A
            naive value would make the comparison depend on the server's local
            timezone.

    Returns:
        ShowtimeRecord: The created showtime, with its movie and screen embedded.

    Raises:
        ApiError: ``MOVIE_NOT_FOUND`` or ``SCREEN_NOT_FOUND`` with status 404,
            ``SHOWTIME_IN_PAST`` with status 422, or ``SHOWTIME_OVERLAP`` with
            status 409.
    """
    movie_row = (
        await conn.execute(
            select(movies.c.id, movies.c.title, movies.c.poster_url).where(
                movies.c.id == data.movie_id
            )
        )
    ).one_or_none()
    if movie_row is None:
        logger.info(
            "showtime_create_rejected reason=movie_not_found movie_id=%s",
            data.movie_id,
        )
        raise _movie_not_found()

    screen_row = (
        await conn.execute(
            select(screens.c.id, screens.c.name).where(screens.c.id == data.screen_id)
        )
    ).one_or_none()
    if screen_row is None:
        logger.info(
            "showtime_create_rejected reason=screen_not_found screen_id=%s",
            data.screen_id,
        )
        raise _screen_not_found()

    starts_at = data.starts_at
    if starts_at <= now:
        logger.info(
            "showtime_create_rejected reason=in_past screen_id=%s",
            data.screen_id,
        )
        raise _showtime_in_past()

    # Serializes concurrent creates on this screen so the overlap check below
    # cannot race (SPEC-3 Section 2.5 step 3). The row already exists -- it was
    # loaded just above -- so the lock always finds it.
    await conn.execute(
        select(screens.c.id).where(screens.c.id == data.screen_id).with_for_update()
    )

    lower, upper = rules.conflict_window(
        starts_at, get_settings().showtime_slot_minutes
    )
    overlapping = (
        await conn.execute(
            select(showtimes.c.id)
            .where(
                showtimes.c.screen_id == data.screen_id,
                showtimes.c.starts_at > lower,
                showtimes.c.starts_at < upper,
            )
            .limit(1)
        )
    ).one_or_none()
    if overlapping is not None:
        logger.info(
            "showtime_create_rejected reason=overlap screen_id=%s",
            data.screen_id,
        )
        raise _showtime_overlap()

    row = (
        await conn.execute(
            insert(showtimes)
            .values(
                movie_id=data.movie_id,
                screen_id=data.screen_id,
                starts_at=starts_at,
                price_cents=data.price_cents,
            )
            .returning(
                showtimes.c.id,
                showtimes.c.starts_at,
                showtimes.c.price_cents,
            )
        )
    ).one()

    logger.info("showtime_created actor=%s showtime_id=%s", actor_id, row.id)
    return ShowtimeRecord(
        id=row.id,
        starts_at=row.starts_at,
        price_cents=row.price_cents,
        movie=ShowtimeMovieRecord(
            id=movie_row.id,
            title=movie_row.title,
            poster_url=movie_row.poster_url,
        ),
        screen=ShowtimeScreenRecord(id=screen_row.id, name=screen_row.name),
    )


async def get_showtime(conn: AsyncConnection, showtime_id: int) -> ShowtimeRecord:
    """Load one showtime with its movie and screen (SPEC-3 Section 2.3).

    Args:
        conn: An open connection. This function only reads.
        showtime_id: The showtime's primary key.

    Returns:
        ShowtimeRecord: The showtime, with its movie and screen embedded from the
        same JOIN.

    Raises:
        ApiError: ``SHOWTIME_NOT_FOUND`` with status 404 when no showtime has
            that id.
    """
    row = (
        await conn.execute(
            select(*_SHOWTIME_COLUMNS)
            .select_from(showtimes)
            .join(movies, movies.c.id == showtimes.c.movie_id)
            .join(screens, screens.c.id == showtimes.c.screen_id)
            .where(showtimes.c.id == showtime_id)
        )
    ).one_or_none()
    if row is None:
        raise _showtime_not_found()
    return _showtime_record(row)


async def list_showtimes(
    conn: AsyncConnection,
    *,
    day: date | None,
    movie_id: int | None,
    limit: int,
    offset: int,
    now: datetime,
) -> tuple[list[ShowtimeRecord], int]:
    """List a page of showtimes for browsing (SPEC-3 Section 2.5, FR-13, FR-14).

    ``day`` selects exactly the showtimes whose start falls on that calendar day
    in ``CINEMA_TIMEZONE``; without it, only showtimes that have not yet started
    are returned. An optional ``movie_id`` narrows the page further, and an
    unknown movie id is not an error -- it simply matches nothing (SPEC-3 Section
    2.5, "Browse showtimes"). Order is ``starts_at, id``, and ``total`` counts
    every matching showtime ignoring ``limit``/``offset``, so a client can compute
    the number of pages.

    The page query embeds the movie and the screen through one JOIN; the count
    query touches ``showtimes`` alone, so no rows are fetched to count them and
    the date/movie indexes stay usable.

    Args:
        conn: An open connection. This function only reads.
        day: The calendar day to browse, or ``None`` for everything from ``now``
            onward.
        movie_id: When given, only showtimes of this movie are returned.
        limit: Maximum number of showtimes to return (already validated 1..100).
        offset: Number of showtimes to skip (already validated >= 0).
        now: The current instant, timezone-aware UTC, passed in rather than read
            from the clock so a test can pin the boundary (SPEC-3 test 41).

    Returns:
        tuple[list[ShowtimeRecord], int]: The page's showtimes, each with its
        movie and screen embedded, and the total number of matching showtimes. A
        day with no showtimes yields an empty list and a total of 0, not an error
        (SPEC-3 Section 3).
    """
    conditions = _showtime_filters(day, movie_id, now)

    rows = (
        await conn.execute(
            select(*_SHOWTIME_COLUMNS)
            .select_from(showtimes)
            .join(movies, movies.c.id == showtimes.c.movie_id)
            .join(screens, screens.c.id == showtimes.c.screen_id)
            .where(*conditions)
            .order_by(showtimes.c.starts_at, showtimes.c.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = (
        await conn.execute(
            select(func.count()).select_from(showtimes).where(*conditions)
        )
    ).scalar_one()

    return [_showtime_record(row) for row in rows], total


async def delete_showtime(
    conn: AsyncConnection,
    showtime_id: int,
    actor_id: int,
    now: datetime,
) -> None:
    """Delete a showtime that has not started and has no reservations (FR-12).

    Deleting is refused once the showtime has started (409 ``SHOWTIME_STARTED``)
    and when any ``reservations`` row references it, whatever that reservation's
    status, because the history is kept (409 ``SHOWTIME_HAS_RESERVATIONS``,
    decision D4). The pre-checks give a clean 409 for the common cases; the
    delete itself runs inside a SAVEPOINT so a reservation inserted concurrently
    -- the case the pre-check cannot see -- surfaces as the same 409 instead of
    poisoning the request transaction, with the ``ON DELETE RESTRICT`` foreign
    key as the safety net.

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        showtime_id: The showtime's primary key.
        actor_id: The id of the signed-in admin performing the write. Used only
            for the audit log line.
        now: The current instant, timezone-aware UTC, passed in rather than read
            from the clock so a test can pin the "already started" boundary
            (SPEC-3 test 37).

    Returns:
        None.

    Raises:
        ApiError: ``SHOWTIME_NOT_FOUND`` with status 404, ``SHOWTIME_STARTED``
            with status 409, or ``SHOWTIME_HAS_RESERVATIONS`` with status 409.
    """
    row = (
        await conn.execute(
            select(showtimes.c.id, showtimes.c.starts_at).where(
                showtimes.c.id == showtime_id
            )
        )
    ).one_or_none()
    if row is None:
        logger.info(
            "showtime_delete_rejected reason=not_found showtime_id=%s", showtime_id
        )
        raise _showtime_not_found()

    if row.starts_at <= now:
        logger.info(
            "showtime_delete_rejected reason=started showtime_id=%s", showtime_id
        )
        raise _showtime_started()

    has_reservations = (
        await conn.execute(
            select(reservations.c.id)
            .where(reservations.c.showtime_id == showtime_id)
            .limit(1)
        )
    ).one_or_none()
    if has_reservations is not None:
        logger.info(
            "showtime_delete_rejected reason=has_reservations showtime_id=%s",
            showtime_id,
        )
        raise _showtime_has_reservations()

    try:
        async with conn.begin_nested():
            await conn.execute(delete(showtimes).where(showtimes.c.id == showtime_id))
    except IntegrityError as error:
        logger.info(
            "showtime_delete_rejected reason=has_reservations showtime_id=%s",
            showtime_id,
        )
        raise _showtime_has_reservations() from error

    logger.info("showtime_deleted actor=%s showtime_id=%s", actor_id, showtime_id)
