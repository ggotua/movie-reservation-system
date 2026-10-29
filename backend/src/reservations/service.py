"""Reservation business rules over an open database connection (SPEC-4, SPEC-5).

This module owns the seat map (FR-10), the hold algorithm (Section 2.5, FR-1
through FR-6), and reservation read, confirm and release (Sections 2.6-2.8,
FR-7 through FR-9), all with FR-11 and FR-12 applied. SPEC-5 adds the caller's
reservation list (Section 2.5) and cancelling a confirmed reservation
(Section 2.6). It is deliberately free of HTTP and FastAPI imports: every
function takes an already-open
:class:`~sqlalchemy.ext.asyncio.AsyncConnection`, never opens one itself and
never commits, so the request-scoped transaction stays owned by
:func:`src.db.engine.get_connection` and the same functions can be driven from
a test connection.

Import direction (SPEC-4 Section 11.1): ``reservations`` may import ``core``
and ``db``. It does not import :mod:`src.showtimes` or :mod:`src.movies`; the
showtime, screen, seat, user and reservation rows it needs are read from
:mod:`src.db.tables` directly.

The hold is safe for two reasons the code below makes explicit:

* the partial unique index ``idx_seat_reservations_active_unique`` (SPEC-1
  2.9) is the final arbiter of a seat conflict, and the insert names exactly its
  predicate in ``ON CONFLICT ... WHERE status IN ('held', 'confirmed')``;
* every transaction locks in a deterministic order -- expired reservations by
  ascending id, then seat rows by ascending ``seat_id`` -- so overlapping holds
  cannot form a lock cycle.

Timing and settings: ``now`` is a timezone-aware instant passed in by the
caller (the router computes it once per request), never read from the clock
here. ``HOLD_MINUTES`` and ``MAX_SEATS_PER_HOLD`` come from settings, the one
place they exist (SPEC-4 Section 10). All expiry arithmetic lives in
:mod:`src.reservations.rules`; this module only chooses the SQL cutoff and
applies the derived state.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Row,
    and_,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from src.core.config import get_settings
from src.core.enums import (
    ACTIVE_RESERVATION_STATUSES,
    DEFAULT_RESERVATION_STATUS,
    RESERVATION_STATUSES,
    ReservationStatus,
)
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
from src.reservations import rules
from src.reservations.schemas import (
    MovieSummary,
    ReservationListItem,
    ReservationOut,
    ScreenRef,
    ScreenSummary,
    SeatMapOut,
    SeatOut,
    SeatRef,
    ShowtimeSummary,
)

logger = logging.getLogger(__name__)

# The SPEC-4 Section 2.4 codes, declared once here so no bare code string is
# scattered through the module (Section 10, DRY check). ``SHOWTIME_STARTED``
# is reused from SPEC-3's vocabulary but repeated as a module-local constant:
# ``reservations`` must not import ``showtimes`` (Section 11.1).
SHOWTIME_NOT_FOUND = "SHOWTIME_NOT_FOUND"
SHOWTIME_STARTED = "SHOWTIME_STARTED"
SEAT_NOT_FOUND = "SEAT_NOT_FOUND"
SEAT_UNAVAILABLE = "SEAT_UNAVAILABLE"
ACTIVE_HOLD_EXISTS = "ACTIVE_HOLD_EXISTS"
RESERVATION_NOT_FOUND = "RESERVATION_NOT_FOUND"
RESERVATION_EXPIRED = "RESERVATION_EXPIRED"
RESERVATION_NOT_HELD = "RESERVATION_NOT_HELD"
RESERVATION_NOT_CONFIRMED = "RESERVATION_NOT_CONFIRMED"
TOO_MANY_SEATS = "TOO_MANY_SEATS"

# The stored statuses this layer writes or compares. All are taken from the
# single vocabulary in ``core/enums.py`` rather than restated as literals:
# ``DEFAULT_RESERVATION_STATUS`` is the initial ``held`` status, and
# ``RESERVATION_STATUSES`` declares the four statuses in the order
# held, confirmed, cancelled, expired.
_HELD_STATUS: ReservationStatus = DEFAULT_RESERVATION_STATUS
_CONFIRMED_STATUS: ReservationStatus = RESERVATION_STATUSES[1]
_CANCELLED_STATUS: ReservationStatus = RESERVATION_STATUSES[2]
_EXPIRED_STATUS: ReservationStatus = RESERVATION_STATUSES[-1]

# The exact predicate of idx_seat_reservations_active_unique (SPEC-1 2.9 and
# migration 0001). It is rendered as SQL text, not as a bound parameter list,
# because PostgreSQL must prove that this ON CONFLICT predicate is the same
# expression as the partial index's cataloged predicate in order to infer the
# index. The values are interpolated from the shared enum tuple, never typed
# as literals here, and are controlled vocabulary values.
_ACTIVE_STATUS_VALUES_SQL = ", ".join(
    f"'{status}'" for status in ACTIVE_RESERVATION_STATUSES
)
_ACTIVE_STATUS_PREDICATE = text(f"status IN ({_ACTIVE_STATUS_VALUES_SQL})")


class _SeatConflictError(Exception):
    """Internal signal that some requested seats could not be inserted.

    It is raised *inside* the hold's SAVEPOINT so the reservation row and any
    seat rows already inserted are rolled back before the caller turns the
    conflict into the public 409. It never leaves this module.

    Attributes:
        seat_ids: The requested seat ids whose insert did not return a row.
    """

    def __init__(self, seat_ids: tuple[int, ...]) -> None:
        """Store the seat ids that conflicted.

        Args:
            seat_ids: The requested ids missing from the insert's
                ``RETURNING`` result.
        """
        super().__init__("seat insert conflict")
        self.seat_ids = seat_ids


def _showtime_not_found() -> ApiError:
    """Build the 404 returned when a showtime id does not exist (FR-4).

    Returns:
        ApiError: ``SHOWTIME_NOT_FOUND`` with status 404.
    """
    return ApiError(SHOWTIME_NOT_FOUND, "No showtime with that id exists.", 404)


def _showtime_started() -> ApiError:
    """Build the 409 returned when a showtime has already started (FR-4).

    Returns:
        ApiError: ``SHOWTIME_STARTED`` with status 409.
    """
    return ApiError(
        SHOWTIME_STARTED,
        "This showtime has already started.",
        409,
    )


def _seat_not_found() -> ApiError:
    """Build the 404 for an unknown or wrong-screen seat (FR-3).

    The same error covers a nonexistent id and a seat belonging to another
    screen so the caller cannot distinguish the two cases (Section 2.4).

    Returns:
        ApiError: ``SEAT_NOT_FOUND`` with status 404.
    """
    return ApiError(
        SEAT_NOT_FOUND,
        "One or more seats do not exist on this showtime's screen.",
        404,
    )


def _too_many_seats(limit: int) -> ApiError:
    """Build the 422 returned when a hold requests too many seats (FR-3).

    Args:
        limit: The configured ``MAX_SEATS_PER_HOLD`` value.

    Returns:
        ApiError: ``TOO_MANY_SEATS`` with status 422.
    """
    return ApiError(
        TOO_MANY_SEATS,
        f"You can hold at most {limit} seats at a time.",
        422,
    )


def _active_hold_exists() -> ApiError:
    """Build the 409 returned for a second hold by one user (FR-6).

    Returns:
        ApiError: ``ACTIVE_HOLD_EXISTS`` with status 409.
    """
    return ApiError(
        ACTIVE_HOLD_EXISTS,
        "You already have an active hold for this showtime.",
        409,
    )


def _reservation_not_found() -> ApiError:
    """Build the 404 returned when a reservation id does not resolve.

    Returns:
        ApiError: ``RESERVATION_NOT_FOUND`` with status 404.
    """
    return ApiError(
        RESERVATION_NOT_FOUND,
        "No reservation with that id exists.",
        404,
    )


def _reservation_expired() -> ApiError:
    """Build the 409 returned when confirming a held reservation too late.

    A stored ``held`` reservation past its computed expiry has not been
    rewritten yet; confirming it writes nothing and lets the next hold's lazy
    expiry clean it up (SPEC-4 Section 2.6).

    Returns:
        ApiError: ``RESERVATION_EXPIRED`` with status 409.
    """
    return ApiError(
        RESERVATION_EXPIRED,
        "This reservation hold has expired.",
        409,
    )


def _reservation_not_held() -> ApiError:
    """Build the 409 returned when a reservation is no longer an active hold.

    Used when confirming a cancelled or already-``expired`` reservation, and
    for releasing anything that is not a held, unexpired reservation (SPEC-4
    Sections 2.6 and 2.7).

    Returns:
        ApiError: ``RESERVATION_NOT_HELD`` with status 409.
    """
    return ApiError(
        RESERVATION_NOT_HELD,
        "This reservation is not an active hold.",
        409,
    )


def _reservation_not_confirmed() -> ApiError:
    """Build the 409 returned when cancelling a non-confirmed reservation.

    Only a confirmed reservation can be cancelled (SPEC-5 D1, FR-7). A held
    reservation is given up with ``release`` (SPEC-4), and a stored
    ``cancelled`` row may have been a released hold, so "already cancelled" is
    deliberately not answered with 200 (SPEC-5 D2).

    Returns:
        ApiError: ``RESERVATION_NOT_CONFIRMED`` with status 409.
    """
    return ApiError(
        RESERVATION_NOT_CONFIRMED,
        "Only confirmed reservations can be cancelled.",
        409,
    )


def _seat_unavailable(seat_ids: Sequence[int]) -> ApiError:
    """Build the 409 that names the seats another hold owns (FR-2, D8).

    Args:
        seat_ids: The seats that are actively held or confirmed. Repeated ids
            are collapsed and the list is sorted so the message is stable.

    Returns:
        ApiError: ``SEAT_UNAVAILABLE`` with status 409 and a message listing
        the unavailable seat ids.
    """
    unavailable = sorted(set(seat_ids))
    if len(unavailable) == 1:
        message = f"Seat {unavailable[0]} is no longer available."
    else:
        listed = ", ".join(str(seat_id) for seat_id in unavailable)
        message = f"Seats {listed} are no longer available."
    return ApiError(SEAT_UNAVAILABLE, message, 409)


def _is_unique_violation(error: IntegrityError) -> bool:
    """Return whether an ``IntegrityError`` is a unique violation (SQLSTATE 23505).

    Args:
        error: The integrity error raised by the database driver.

    Returns:
        bool: ``True`` when the driver reported SQLSTATE ``23505``. Other
        integrity failures (for example a foreign-key violation) are not
        seat conflicts and must keep propagating.
    """
    return getattr(error.orig, "sqlstate", None) == "23505"


async def _active_seat_ids(
    conn: AsyncConnection,
    showtime_id: int,
    seat_ids: Sequence[int],
) -> list[int]:
    """Read the seats of one showtime that currently have an active row.

    Called only after a savepoint rollback, so it sees other transactions'
    committed rows and none of the failed transaction's rows. It is used to
    name the unavailable seats in the 409 message (D8).

    Args:
        conn: An open connection whose failed savepoint has been rolled back.
        showtime_id: The showtime the conflict belongs to.
        seat_ids: The seats to inspect.

    Returns:
        list[int]: The subset of ``seat_ids`` with a ``held`` or ``confirmed``
        ``seat_reservations`` row, in ascending seat-id order.
    """
    if not seat_ids:
        return []
    rows = (
        await conn.execute(
            select(seat_reservations.c.seat_id)
            .where(
                seat_reservations.c.showtime_id == showtime_id,
                seat_reservations.c.seat_id.in_(seat_ids),
                seat_reservations.c.status.in_(ACTIVE_RESERVATION_STATUSES),
            )
            .order_by(seat_reservations.c.seat_id)
        )
    ).all()
    return [row.seat_id for row in rows]


async def get_seat_map(
    conn: AsyncConnection,
    showtime_id: int,
    now: datetime,
) -> SeatMapOut:
    """Return the public seat map of one showtime (FR-10, D6).

    Every seat of the showtime's screen is returned, ordered by row label then
    seat number. One query loads the showtime and its screen, and one query
    loads all seats with a LEFT JOIN to the seat's *active* reservation row and
    its parent reservation, so there is no N+1 read and no expired or terminal
    row can block a seat.

    This function is read-only: it never updates an expired hold. Expiry is
    derived through :func:`src.reservations.rules.seat_state`, and no user id
    (or any other holder information) is selected or returned.

    Args:
        conn: An open connection. This function only reads and never commits.
        showtime_id: The showtime whose seat map is requested.
        now: The current instant, timezone-aware, used to derive hold expiry.

    Returns:
        SeatMapOut: The showtime id, the screen's identity and dimensions, and
        every seat of that screen with its ``available`` / ``held`` / ``taken``
        state.

    Raises:
        ApiError: ``SHOWTIME_NOT_FOUND`` with status 404 when no showtime has
            that id. An unknown showtime is checked before any seat is read.
    """
    screen_row = (
        await conn.execute(
            select(
                screens.c.id,
                screens.c.name,
                screens.c.rows,
                screens.c.seats_per_row,
            )
            .select_from(showtimes.join(screens, screens.c.id == showtimes.c.screen_id))
            .where(showtimes.c.id == showtime_id)
        )
    ).one_or_none()
    if screen_row is None:
        raise _showtime_not_found()

    hold_minutes = get_settings().hold_minutes
    seat_rows = (
        await conn.execute(
            select(
                seats.c.id,
                seats.c.row_label,
                seats.c.seat_number,
                seats.c.seat_type,
                seat_reservations.c.status,
                reservations.c.created_at,
            )
            .select_from(
                seats.outerjoin(
                    seat_reservations,
                    and_(
                        seat_reservations.c.seat_id == seats.c.id,
                        seat_reservations.c.showtime_id == showtime_id,
                        seat_reservations.c.status.in_(ACTIVE_RESERVATION_STATUSES),
                    ),
                ).outerjoin(
                    reservations,
                    reservations.c.id == seat_reservations.c.reservation_id,
                )
            )
            .where(seats.c.screen_id == screen_row.id)
            .order_by(seats.c.row_label, seats.c.seat_number)
        )
    ).all()

    return SeatMapOut(
        showtime_id=showtime_id,
        screen=ScreenRef(
            id=screen_row.id,
            name=screen_row.name,
            rows=screen_row.rows,
            seats_per_row=screen_row.seats_per_row,
        ),
        seats=[
            SeatOut(
                seat_id=row.id,
                row_label=row.row_label,
                seat_number=row.seat_number,
                seat_type=row.seat_type,
                state=rules.seat_state(
                    row.status,
                    row.created_at,
                    now,
                    hold_minutes,
                ),
            )
            for row in seat_rows
        ],
    )


async def hold(
    conn: AsyncConnection,
    user_id: int,
    showtime_id: int,
    seat_ids: list[int],
    now: datetime,
) -> ReservationOut:
    """Hold the requested seats of a showtime for one user (SPEC-4 2.5).

    The eight steps of SPEC-4 Section 2.5 run in this order, all inside the
    caller's transaction:

    1. load the showtime (404) and refuse one that has started (409);
    2. refuse more than ``MAX_SEATS_PER_HOLD`` seats (422);
    3. check every seat exists on the showtime's screen (404);
    4. lock the user row, serializing this user's simultaneous holds so FR-6
       cannot be raced;
    5. lazily expire the showtime's stale holds and all their seat rows,
       locking them in ascending id order;
    6. refuse a remaining active hold by the same user (409);
    7. insert the reservation and all seat rows in a SAVEPOINT, using one
       ``INSERT ... ON CONFLICT ... DO NOTHING RETURNING`` statement in
       ascending ``seat_id`` order; a short result rolls the savepoint back and
       becomes 409 ``SEAT_UNAVAILABLE``;
    8. return the stored reservation in the public shape.

    The partial unique index is the final arbiter: two transactions inserting
    the same ``(showtime_id, seat_id)`` cannot both succeed. The loser's
    savepoint rolls back both the reservation and every seat row, so no partial
    hold is observable. An escaping unique violation is mapped to the same 409
    as defence in depth.

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        user_id: The authenticated user placing the hold.
        showtime_id: The showtime the seats belong to.
        seat_ids: The requested seat ids. The request schema guarantees they
            are distinct, non-empty strict integers; the service still verifies
            that every id exists on this showtime's screen.
        now: The current instant, timezone-aware, used both for the
            started-showtime check and as the inserted ``created_at``.

    Returns:
        ReservationOut: The new ``held`` reservation with its seats ordered by
        row and number, ``total_cents`` and its derived ``expires_at``.

    Raises:
        ApiError: ``SHOWTIME_NOT_FOUND`` (404), ``SHOWTIME_STARTED`` (409),
            ``TOO_MANY_SEATS`` (422), ``SEAT_NOT_FOUND`` (404),
            ``ACTIVE_HOLD_EXISTS`` (409) or ``SEAT_UNAVAILABLE`` (409).
    """
    settings = get_settings()

    # 1. Showtime: existence first, then the start boundary.
    showtime_row = (
        await conn.execute(
            select(showtimes.c.screen_id, showtimes.c.starts_at).where(
                showtimes.c.id == showtime_id
            )
        )
    ).one_or_none()
    if showtime_row is None:
        raise _showtime_not_found()
    if showtime_row.starts_at <= now:
        raise _showtime_started()

    # 2. Count before touching any seat row.
    if len(seat_ids) > settings.max_seats_per_hold:
        raise _too_many_seats(settings.max_seats_per_hold)

    # 3. Every requested seat must exist on this showtime's screen.
    existing_seat_ids = (
        await conn.execute(
            select(seats.c.id).where(
                seats.c.id.in_(seat_ids),
                seats.c.screen_id == showtime_row.screen_id,
            )
        )
    ).all()
    if len(existing_seat_ids) < len(seat_ids):
        raise _seat_not_found()

    # 4. Serialize this user's holds. The lock is held until the caller
    #    commits, so step 6 cannot miss a simultaneous hold by the same user.
    await conn.execute(
        select(users.c.id).where(users.c.id == user_id).with_for_update()
    )

    # 5. Lazy expiry. Lock in ascending id order, then update the parent rows
    #    before their seat rows so a reservation and its seats match (FR-11).
    cutoff = rules.hold_cutoff(now, settings.hold_minutes)
    expired_ids = [
        row.id
        for row in (
            await conn.execute(
                select(reservations.c.id)
                .where(
                    reservations.c.showtime_id == showtime_id,
                    reservations.c.status == _HELD_STATUS,
                    reservations.c.created_at <= cutoff,
                )
                .order_by(reservations.c.id)
                .with_for_update()
            )
        ).all()
    ]
    if expired_ids:
        await conn.execute(
            update(reservations)
            .where(reservations.c.id.in_(expired_ids))
            .values(status=_EXPIRED_STATUS)
        )
        await conn.execute(
            update(seat_reservations)
            .where(seat_reservations.c.reservation_id.in_(expired_ids))
            .values(status=_EXPIRED_STATUS)
        )
        logger.info(
            "hold_lazy_expiry showtime_id=%s expired_count=%s",
            showtime_id,
            len(expired_ids),
        )

    # 6. One active hold per user per showtime (D3, FR-6).
    existing_hold = (
        await conn.execute(
            select(reservations.c.id)
            .where(
                reservations.c.user_id == user_id,
                reservations.c.showtime_id == showtime_id,
                reservations.c.status == _HELD_STATUS,
            )
            .limit(1)
        )
    ).one_or_none()
    if existing_hold is not None:
        logger.info(
            "hold_rejected reason=active_hold_exists user_id=%s showtime_id=%s",
            user_id,
            showtime_id,
        )
        raise _active_hold_exists()

    # 7. Insert the reservation and all seat rows atomically. Any failure
    #    inside the SAVEPOINT rolls back the reservation row too.
    ordered_seat_ids = sorted(seat_ids)
    try:
        async with conn.begin_nested():
            reservation_id = (
                await conn.execute(
                    insert(reservations)
                    .values(
                        user_id=user_id,
                        showtime_id=showtime_id,
                        status=_HELD_STATUS,
                        created_at=now,
                    )
                    .returning(reservations.c.id)
                )
            ).scalar_one()

            inserted_ids = [
                row.seat_id
                for row in (
                    await conn.execute(
                        pg_insert(seat_reservations)
                        .values(
                            [
                                {
                                    "reservation_id": reservation_id,
                                    "showtime_id": showtime_id,
                                    "seat_id": seat_id,
                                    "status": _HELD_STATUS,
                                }
                                for seat_id in ordered_seat_ids
                            ]
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                seat_reservations.c.showtime_id,
                                seat_reservations.c.seat_id,
                            ],
                            # Must match idx_seat_reservations_active_unique
                            # exactly (SPEC-1 2.9 and the Alembic migration).
                            index_where=_ACTIVE_STATUS_PREDICATE,
                        )
                        .returning(seat_reservations.c.seat_id)
                    )
                ).all()
            ]

            if len(inserted_ids) < len(ordered_seat_ids):
                inserted = set(inserted_ids)
                missing = tuple(
                    seat_id for seat_id in ordered_seat_ids if seat_id not in inserted
                )
                raise _SeatConflictError(missing)
    except _SeatConflictError as conflict:
        # The savepoint has rolled back: query the committed active rows to
        # name the seats (D8), then report the public conflict.
        unavailable = await _active_seat_ids(conn, showtime_id, conflict.seat_ids)
        logger.info(
            "hold_rejected reason=seat_unavailable user_id=%s showtime_id=%s",
            user_id,
            showtime_id,
        )
        raise _seat_unavailable(unavailable or conflict.seat_ids) from None
    except IntegrityError as error:
        if not _is_unique_violation(error):
            raise
        # Defence in depth: the partial unique index rejected a row despite
        # ON CONFLICT DO NOTHING. The savepoint is already rolled back.
        unavailable = await _active_seat_ids(conn, showtime_id, ordered_seat_ids)
        logger.info(
            "hold_rejected reason=seat_unavailable user_id=%s showtime_id=%s",
            user_id,
            showtime_id,
        )
        raise _seat_unavailable(unavailable or ordered_seat_ids) from error

    # 8. The helper reads the row back inside the same transaction, so the
    #    returned seats and total are exactly what was stored.
    logger.info(
        "hold_created user_id=%s reservation_id=%s showtime_id=%s seat_count=%s",
        user_id,
        reservation_id,
        showtime_id,
        len(ordered_seat_ids),
    )
    return await _reservation_out(conn, reservation_id, now)


async def get_reservation(
    conn: AsyncConnection,
    user_id: int,
    reservation_id: int,
    now: datetime,
) -> ReservationOut:
    """Return one reservation owned by the caller (FR-9, FR-12).

    The ownership filter is part of the lookup, so a nonexistent id and another
    user's id take exactly the same path and produce the same 404 (D4). Reads
    never write: the effective status and expiry are derived by
    :func:`_reservation_out` without updating an expired hold (Section 2.5).

    Args:
        conn: An open connection. This function only reads and never commits.
        user_id: The authenticated user; the reservation must belong to them.
        reservation_id: The reservation's primary key.
        now: The current instant, timezone-aware, used to derive hold expiry.

    Returns:
        ReservationOut: The owned reservation with its effective status, seats
        ordered by row label then seat number, and computed ``total_cents``.

    Raises:
        ApiError: ``RESERVATION_NOT_FOUND`` with status 404 when the reservation
            does not exist or belongs to another user.
    """
    owned_row = (
        await conn.execute(
            select(reservations.c.id).where(
                reservations.c.id == reservation_id,
                reservations.c.user_id == user_id,
            )
        )
    ).one_or_none()
    if owned_row is None:
        raise _reservation_not_found()

    return await _reservation_out(conn, reservation_id, now)


async def list_reservations(
    conn: AsyncConnection,
    user_id: int,
    status: ReservationStatus | None,
    upcoming: bool | None,
    limit: int,
    offset: int,
    now: datetime,
) -> tuple[list[ReservationListItem], int]:
    """Return one page of the caller's reservations, newest first (FR-1-FR-5).

    One query reads the page (reservations joined to their showtime, movie and
    screen) and one more reads the seats of exactly those reservations, so the
    page costs two round trips regardless of how many rows it holds -- there is
    no per-reservation seat query. A third query counts every reservation that
    matches the same filters, ignoring ``limit`` / ``offset``, so the caller can
    build the page envelope (SPEC-5 Section 2.5).

    The ownership filter is part of every query: there is no parameter that
    selects another user's rows (FR-1). Reads never write, so a stale ``held``
    row is filtered and reported as ``expired`` through
    :func:`src.reservations.rules.effective_status` and
    :func:`src.reservations.rules.hold_cutoff`; it is left in storage for the
    next hold's lazy expiry (SPEC-4 Section 2.5).

    Args:
        conn: An open connection. This function only reads and never commits.
        user_id: The authenticated user; only rows they own are considered.
        status: Optional filter on the *effective* status. ``held`` keeps
            unexpired held rows, ``expired`` keeps stored-expired rows as well
            as held rows at or past the cutoff, and ``confirmed`` /
            ``cancelled`` compare the stored value. ``None`` applies no status
            filter (D4).
        upcoming: Optional filter on the showtime start (FR-4): ``True`` keeps
            showtimes starting after ``now``, ``False`` keeps showtimes that
            have started (``starts_at <= now``), ``None`` keeps both.
        limit: Maximum number of rows to return; the caller's validated page
            size (1-100).
        offset: Number of rows to skip before the page starts (``>= 0``).
        now: The current instant, timezone-aware. One value per request, so the
            status filter, the effective status and ``expires_at`` agree.

    Returns:
        tuple[list[ReservationListItem], int]: The page's items in
        ``created_at DESC, id DESC`` order with their seats ordered by row
        label then seat number, and the total number of matching reservations
        before pagination.

    Raises:
        ValueError: If ``now`` is naive; raised by
            :func:`src.reservations.rules.hold_cutoff` as in SPEC-4. An empty
            page is not an error: it is returned as an empty list and a
            ``total`` of zero.
    """
    settings = get_settings()
    hold_minutes = settings.hold_minutes
    # One cutoff for both the filter and the count, so they cannot disagree
    # about where a hold expires (SPEC-5 Section 2.5, DRY check).
    cutoff = rules.hold_cutoff(now, hold_minutes)

    filters: list[ColumnElement[bool]] = [reservations.c.user_id == user_id]
    # The effective status is filtered through the one shared SQL definition
    # (SPEC-6 D7), the same condition the admin list of SPEC-6 uses.
    if status is not None:
        filters.append(effective_status_condition(status, cutoff))
    if upcoming is True:
        filters.append(showtimes.c.starts_at > now)
    elif upcoming is False:
        filters.append(showtimes.c.starts_at <= now)

    page_rows = (
        await conn.execute(
            select(
                reservations.c.id,
                reservations.c.showtime_id,
                reservations.c.status,
                reservations.c.created_at,
                reservations.c.confirmed_at,
                reservations.c.cancelled_at,
                showtimes.c.starts_at,
                showtimes.c.price_cents,
                movies.c.id.label("movie_id"),
                movies.c.title,
                movies.c.poster_url,
                screens.c.id.label("screen_id"),
                screens.c.name.label("screen_name"),
            )
            .select_from(
                reservations.join(
                    showtimes, showtimes.c.id == reservations.c.showtime_id
                )
                .join(movies, movies.c.id == showtimes.c.movie_id)
                .join(screens, screens.c.id == showtimes.c.screen_id)
            )
            .where(*filters)
            .order_by(reservations.c.created_at.desc(), reservations.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    # One extra query for the whole page's seats, grouped in Python. The ids
    # come from the page itself, so a page with no rows skips the query
    # entirely instead of asking for ``IN ()``.
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

    # The count joins the showtime too, because the ``upcoming`` filter reads
    # ``showtimes.starts_at``; the inner joins on foreign keys never change the
    # number of reservation rows.
    total = (
        await conn.execute(
            select(func.count())
            .select_from(
                reservations.join(
                    showtimes, showtimes.c.id == reservations.c.showtime_id
                )
            )
            .where(*filters)
        )
    ).scalar_one()

    items: list[ReservationListItem] = []
    for row in page_rows:
        # The single effective-status rule (SPEC-5 FR-3); nothing is written.
        effective = rules.effective_status(
            row.status,
            row.created_at,
            now,
            hold_minutes,
        )
        reservation_seats = seats_by_reservation.get(row.id, [])
        # ``expires_at`` is meaningful only while the reservation is still
        # effectively held (SPEC-4 Section 2.3).
        expires_at: datetime | None = (
            rules.hold_expires_at(row.created_at, hold_minutes)
            if effective == _HELD_STATUS
            else None
        )
        items.append(
            ReservationListItem(
                id=row.id,
                showtime_id=row.showtime_id,
                status=effective,
                seats=reservation_seats,
                total_cents=row.price_cents * len(reservation_seats),
                created_at=row.created_at,
                expires_at=expires_at,
                confirmed_at=row.confirmed_at,
                cancelled_at=row.cancelled_at,
                showtime=ShowtimeSummary(
                    id=row.showtime_id,
                    starts_at=row.starts_at,
                    price_cents=row.price_cents,
                    movie=MovieSummary(
                        id=row.movie_id,
                        title=row.title,
                        poster_url=row.poster_url,
                    ),
                    screen=ScreenSummary(id=row.screen_id, name=row.screen_name),
                ),
            )
        )
    return items, total


async def _lock_owned_reservation(
    conn: AsyncConnection,
    user_id: int,
    reservation_id: int,
) -> Row[*tuple[Any, ...]]:
    """Lock one reservation row for update and enforce ownership (D4).

    Every writer that mutates a reservation -- confirm, release and cancel --
    starts here, so "missing" and "belongs to another user" are one 404
    (SPEC-4 D4) and every writer takes the parent row lock before touching
    seat rows, in the same order as lazy expiry. A second transaction on the
    same reservation waits here and then sees the state the first committed,
    which is what makes two simultaneous cancels resolve to one success
    (SPEC-5 FR-9).

    Args:
        conn: An open connection. The caller owns the transaction; this helper
            neither commits nor releases the lock.
        user_id: The authenticated user; a row owned by someone else is
            treated as missing.
        reservation_id: The reservation's primary key.

    Returns:
        Row: The locked row with ``id``, ``showtime_id``, ``status`` and
        ``created_at``, exactly the columns the callers decide on.

    Raises:
        ApiError: ``RESERVATION_NOT_FOUND`` with status 404 when the
            reservation does not exist or belongs to another user.
    """
    reservation_row = (
        await conn.execute(
            select(
                reservations.c.id,
                reservations.c.showtime_id,
                reservations.c.status,
                reservations.c.created_at,
            )
            .where(
                reservations.c.id == reservation_id,
                reservations.c.user_id == user_id,
            )
            .with_for_update()
        )
    ).one_or_none()
    if reservation_row is None:
        raise _reservation_not_found()
    return reservation_row


async def confirm(
    conn: AsyncConnection,
    user_id: int,
    reservation_id: int,
    now: datetime,
) -> ReservationOut:
    """Confirm one held reservation owned by the caller (SPEC-4 Section 2.6).

    The reservation row is locked before any decision, so concurrent confirm,
    release and lazy-expiry operations on the same reservation serialize.
    Confirming an already confirmed reservation is idempotent and returns its
    current state unchanged (D7). A held reservation past its derived expiry is
    refused without writing; the next hold's lazy expiry remains responsible
    for persisting ``expired``.

    Args:
        conn: An open connection. The caller owns the transaction and commit;
            this function never commits.
        user_id: The authenticated user; the reservation must belong to them.
        reservation_id: The reservation's primary key.
        now: The current instant, timezone-aware. Used for expiry, the
            showtime-started check, and the stored ``confirmed_at``.

    Returns:
        ReservationOut: The reservation in its confirmed state, with its seats
        ordered by row label then seat number and computed ``total_cents``.

    Raises:
        ApiError: ``RESERVATION_NOT_FOUND`` (404) when the reservation does not
            exist or belongs to another user; ``RESERVATION_NOT_HELD`` (409)
            for a cancelled or stored-``expired`` reservation;
            ``RESERVATION_EXPIRED`` (409) for a held reservation past its
            expiry; or ``SHOWTIME_STARTED`` (409) when the showtime has started.
    """
    settings = get_settings()
    reservation_row = await _lock_owned_reservation(conn, user_id, reservation_id)

    if reservation_row.status == _CONFIRMED_STATUS:
        reservation = await _reservation_out(conn, reservation_id, now)
        logger.info(
            "reservation_confirmed user_id=%s reservation_id=%s showtime_id=%s "
            "seat_count=%s",
            user_id,
            reservation_id,
            reservation_row.showtime_id,
            len(reservation.seats),
        )
        return reservation

    if reservation_row.status in (_CANCELLED_STATUS, _EXPIRED_STATUS):
        logger.info(
            "reservation_confirm_rejected reason=not_held user_id=%s showtime_id=%s",
            user_id,
            reservation_row.showtime_id,
        )
        raise _reservation_not_held()

    if rules.is_hold_expired(
        reservation_row.created_at,
        now,
        settings.hold_minutes,
    ):
        logger.info(
            "reservation_confirm_rejected reason=expired user_id=%s showtime_id=%s",
            user_id,
            reservation_row.showtime_id,
        )
        raise _reservation_expired()

    starts_at = (
        await conn.execute(
            select(showtimes.c.starts_at).where(
                showtimes.c.id == reservation_row.showtime_id
            )
        )
    ).scalar_one()
    if starts_at <= now:
        logger.info(
            "reservation_confirm_rejected reason=showtime_started user_id=%s "
            "showtime_id=%s",
            user_id,
            reservation_row.showtime_id,
        )
        raise _showtime_started()

    # Lock order matches lazy expiry and release: the reservation row first,
    # then its seat rows. Every writer queues on the parent before touching a
    # child, so overlapping operations cannot form a parent/child lock cycle.
    # Payment seam: real payment would happen here before the hold is confirmed.
    await conn.execute(
        update(reservations)
        .where(reservations.c.id == reservation_id)
        .values(status=_CONFIRMED_STATUS, confirmed_at=now)
    )
    await conn.execute(
        update(seat_reservations)
        .where(seat_reservations.c.reservation_id == reservation_id)
        .values(status=_CONFIRMED_STATUS)
    )

    reservation = await _reservation_out(conn, reservation_id, now)
    logger.info(
        "reservation_confirmed user_id=%s reservation_id=%s showtime_id=%s "
        "seat_count=%s",
        user_id,
        reservation_id,
        reservation_row.showtime_id,
        len(reservation.seats),
    )
    return reservation


async def release(
    conn: AsyncConnection,
    user_id: int,
    reservation_id: int,
    now: datetime,
) -> ReservationOut:
    """Release one held reservation owned by the caller (Section 2.7, FR-8).

    Only an unexpired held reservation may be released. A held reservation past
    its derived expiry is already effectively free, so it is refused rather
    than rewritten; the next hold's lazy expiry persists its ``expired`` state.

    Args:
        conn: An open connection. The caller owns the transaction and commit;
            this function never commits.
        user_id: The authenticated user; the reservation must belong to them.
        reservation_id: The reservation's primary key.
        now: The current instant, timezone-aware, used to derive hold expiry and
            stored as ``cancelled_at``.

    Returns:
        ReservationOut: The cancelled reservation with its seats ordered by row
        label then seat number and computed ``total_cents``.

    Raises:
        ApiError: ``RESERVATION_NOT_FOUND`` with status 404 when the reservation
            does not exist or belongs to another user, or
            ``RESERVATION_NOT_HELD`` with status 409 when it is not a held,
            unexpired reservation.
    """
    settings = get_settings()
    reservation_row = await _lock_owned_reservation(conn, user_id, reservation_id)

    if reservation_row.status != _HELD_STATUS or rules.is_hold_expired(
        reservation_row.created_at,
        now,
        settings.hold_minutes,
    ):
        logger.info(
            "reservation_release_rejected reason=not_held user_id=%s showtime_id=%s",
            user_id,
            reservation_row.showtime_id,
        )
        raise _reservation_not_held()

    # Same lock order as confirm and lazy expiry: the reservation row is locked
    # first and its seat rows second, preventing a parent/child lock cycle.
    await conn.execute(
        update(reservations)
        .where(reservations.c.id == reservation_id)
        .values(status=_CANCELLED_STATUS, cancelled_at=now)
    )
    await conn.execute(
        update(seat_reservations)
        .where(seat_reservations.c.reservation_id == reservation_id)
        .values(status=_CANCELLED_STATUS)
    )

    reservation = await _reservation_out(conn, reservation_id, now)
    logger.info(
        "reservation_released user_id=%s reservation_id=%s showtime_id=%s "
        "seat_count=%s",
        user_id,
        reservation_id,
        reservation_row.showtime_id,
        len(reservation.seats),
    )
    return reservation


async def cancel(
    conn: AsyncConnection,
    user_id: int,
    reservation_id: int,
    now: datetime,
) -> ReservationOut:
    """Cancel one confirmed reservation owned by the caller (SPEC-5 2.6).

    Only a confirmed reservation of a showtime that has not started may be
    cancelled (D1, D3). The reservation row is locked before any decision, so
    two simultaneous cancels serialize and exactly one succeeds (FR-9); the
    other sees ``cancelled`` and is refused with a 409 rather than answered
    with 200, because cancel is deliberately not idempotent (D2).

    The reservation row is written first and its seat rows second -- the same
    lock order as confirm, release and lazy expiry -- so no state exists where
    the reservation is cancelled while its seats are still blocked. Both
    updates, and therefore the removal of the seat rows from the partial
    unique index, commit together; cancelled rows are kept, never deleted
    (D6, FR-11).

    Args:
        conn: An open connection. The caller owns the transaction and commit;
            this function never commits.
        user_id: The authenticated user; the reservation must belong to them.
        reservation_id: The reservation's primary key.
        now: The current instant, timezone-aware. Used as the start boundary
            (``starts_at <= now`` is refused) and stored as ``cancelled_at``.

    Returns:
        ReservationOut: The reservation in its cancelled state, with its seats
        ordered by row label then seat number and computed ``total_cents``.

    Raises:
        ApiError: ``RESERVATION_NOT_FOUND`` (404) when the reservation does not
            exist or belongs to another user; ``RESERVATION_NOT_CONFIRMED``
            (409) when its stored status is not ``confirmed``; or
            ``SHOWTIME_STARTED`` (409) when the showtime has already started.
    """
    reservation_row = await _lock_owned_reservation(conn, user_id, reservation_id)

    if reservation_row.status != _CONFIRMED_STATUS:
        logger.info(
            "reservation_cancel_rejected reason=not_confirmed user_id=%s "
            "reservation_id=%s",
            user_id,
            reservation_id,
        )
        raise _reservation_not_confirmed()

    starts_at = (
        await conn.execute(
            select(showtimes.c.starts_at).where(
                showtimes.c.id == reservation_row.showtime_id
            )
        )
    ).scalar_one()
    if starts_at <= now:
        logger.info(
            "reservation_cancel_rejected reason=showtime_started user_id=%s "
            "reservation_id=%s showtime_id=%s",
            user_id,
            reservation_id,
            reservation_row.showtime_id,
        )
        raise _showtime_started()

    # Reservation first, then its seat rows: the same lock order as confirm,
    # release and lazy expiry, so writers cannot form a parent/child cycle.
    await conn.execute(
        update(reservations)
        .where(reservations.c.id == reservation_id)
        .values(status=_CANCELLED_STATUS, cancelled_at=now)
    )
    await conn.execute(
        update(seat_reservations)
        .where(seat_reservations.c.reservation_id == reservation_id)
        .values(status=_CANCELLED_STATUS)
    )

    reservation = await _reservation_out(conn, reservation_id, now)
    logger.info(
        "reservation_cancelled user_id=%s reservation_id=%s showtime_id=%s "
        "seat_count=%s",
        user_id,
        reservation_id,
        reservation_row.showtime_id,
        len(reservation.seats),
    )
    return reservation


async def _reservation_out(
    conn: AsyncConnection,
    reservation_id: int,
    now: datetime,
) -> ReservationOut:
    """Load one reservation and shape it for the API (SPEC-4 Section 2.3).

    The stored ``held`` status becomes the *effective* status: a hold at or
    past its expiry is reported as ``expired`` without writing anything, so
    reads never mutate rows (Section 2.5). ``expires_at`` is set only for an
    effectively held reservation; every other status reports ``None``.

    This is the single read-back helper shared by the hold and, later, the
    confirm / release / read operations. It does not check ownership -- that
    belongs to the caller's rule (D4) -- and selects no user identity.

    Args:
        conn: An open connection. This function only reads and never commits.
        reservation_id: The reservation's primary key.
        now: The current instant, timezone-aware, used to derive expiry.

    Returns:
        ReservationOut: The reservation with its seats ordered by row label
        then seat number, and ``total_cents`` computed from the showtime price.

    Raises:
        ApiError: ``RESERVATION_NOT_FOUND`` with status 404 when no reservation
            has that id.
    """
    reservation_row = (
        await conn.execute(
            select(
                reservations.c.id,
                reservations.c.showtime_id,
                reservations.c.status,
                reservations.c.created_at,
                reservations.c.confirmed_at,
                reservations.c.cancelled_at,
                showtimes.c.price_cents,
            )
            .select_from(
                reservations.join(
                    showtimes, showtimes.c.id == reservations.c.showtime_id
                )
            )
            .where(reservations.c.id == reservation_id)
        )
    ).one_or_none()
    if reservation_row is None:
        raise _reservation_not_found()

    seat_rows = (
        await conn.execute(
            select(seats.c.id, seats.c.row_label, seats.c.seat_number)
            .select_from(
                seat_reservations.join(seats, seats.c.id == seat_reservations.c.seat_id)
            )
            .where(seat_reservations.c.reservation_id == reservation_id)
            .order_by(seats.c.row_label, seats.c.seat_number)
        )
    ).all()

    hold_minutes = get_settings().hold_minutes
    # The effective-status rule is defined once, in ``rules.effective_status``
    # (SPEC-5 FR-3); this read-back only applies it.
    status = rules.effective_status(
        reservation_row.status,
        reservation_row.created_at,
        now,
        hold_minutes,
    )
    # ``expires_at`` is derived from the same window and is reported only while
    # the reservation is still effectively held (SPEC-4 Section 2.3).
    expires_at: datetime | None = (
        rules.hold_expires_at(reservation_row.created_at, hold_minutes)
        if status == _HELD_STATUS
        else None
    )

    return ReservationOut(
        id=reservation_row.id,
        showtime_id=reservation_row.showtime_id,
        status=status,
        seats=[
            SeatRef(
                seat_id=row.id,
                row_label=row.row_label,
                seat_number=row.seat_number,
            )
            for row in seat_rows
        ],
        total_cents=reservation_row.price_cents * len(seat_rows),
        created_at=reservation_row.created_at,
        expires_at=expires_at,
        confirmed_at=reservation_row.confirmed_at,
        cancelled_at=reservation_row.cancelled_at,
    )
