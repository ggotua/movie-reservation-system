"""Thin HTTP handlers for seat maps and reservations (SPEC-4 Section 2.3).

Every handler only parses its inputs, calls the matching function in
:mod:`src.reservations.service` and returns the service's response model; the
seat-map derivation, hold transaction, ownership checks and every other business
rule live in the service (``docs/steering/conventions.md`` and
``principles.md``: "Business rules never live inside a FastAPI route handler").

Routes:

    GET  /showtimes/{showtime_id}/seats          public
    POST /reservations                           authenticated, 201
    GET  /reservations                           authenticated, owner only
    GET  /reservations/{reservation_id}          authenticated, owner only
    POST /reservations/{reservation_id}/confirm  authenticated, owner only
    POST /reservations/{reservation_id}/release  authenticated, owner only
    POST /reservations/{reservation_id}/cancel   authenticated, owner only

The protected routes obtain the caller through
:func:`~src.auth.dependencies.get_current_user`; the ownership rule itself stays
inside the service so it holds even when the function is called from another
module (SPEC-4 Section 2.8, FR-9). The seat map is public and never identifies a
holder (FR-10).

``now`` is read from the clock once per request and passed to the service, so
every check in a request uses one instant (SPEC-4 Section 2.2). Failures are
raised as :class:`~src.core.errors.ApiError` by the service or the auth
dependency and rendered by the handlers installed in ``main.py``; no handler
builds an error response itself, which keeps the standard error shape defined in
exactly one place (FR-14).

Connections are requested through :data:`~src.db.engine.DbConnection`, the one
alias that carries ``scope="function"``, so a write is committed before the
response is sent and each authenticated handler shares one connection and one
transaction with :func:`~src.auth.dependencies.get_current_user`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status

from src.auth.dependencies import get_current_user
from src.auth.service import UserRecord
from src.core.enums import ReservationStatus
from src.core.pagination import Page, PageParams, get_page_params
from src.db.engine import DbConnection
from src.reservations import service
from src.reservations.schemas import (
    HoldRequest,
    ListParams,
    ReservationListItem,
    ReservationOut,
    SeatMapOut,
)

# One router for the whole SPEC-4 surface. The seat-map path is public; each
# /reservations route declares get_current_user itself so the handler receives
# the authenticated user id that the ownership-filtered service needs.
router = APIRouter(tags=["reservations"])


@router.get("/showtimes/{showtime_id}/seats", response_model=SeatMapOut)
async def read_seat_map(showtime_id: int, conn: DbConnection) -> SeatMapOut:
    """Return the public seat map of one showtime (SPEC-4 FR-10).

    Args:
        showtime_id: The showtime's primary key, taken from the path.
        conn: The request-scoped connection; this route only reads.

    Returns:
        SeatMapOut: The screen's identity and every seat with its derived
        ``available`` / ``held`` / ``taken`` state. An expired hold is reported
        as available and no holder identity is included (FR-10).

    Raises:
        ApiError: ``SHOWTIME_NOT_FOUND`` with status 404 when no showtime has
        that id. The service raises it; ``main.py`` renders the standard body.
    """
    return await service.get_seat_map(conn, showtime_id, datetime.now(UTC))


@router.post(
    "/reservations",
    response_model=ReservationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_hold(
    payload: HoldRequest,
    user: Annotated[UserRecord, Depends(get_current_user)],
    conn: DbConnection,
) -> ReservationOut:
    """Create a held reservation for the authenticated user (SPEC-4 FR-1).

    Args:
        payload: The validated hold body. ``seat_ids`` is already non-empty,
            distinct and made of strict integers; the configured maximum and
            every seat/ownership rule are service concerns.
        user: The authenticated user, resolved by ``get_current_user`` and used
            as the reservation's owner.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        ReservationOut: The new ``held`` reservation, including its seats,
        ``total_cents`` and derived ``expires_at``, serialised through the
        declared response model.

    Raises:
        ApiError: The SPEC-4 hold failures (``SHOWTIME_NOT_FOUND``,
            ``SHOWTIME_STARTED``, ``TOO_MANY_SEATS``, ``SEAT_NOT_FOUND``,
            ``ACTIVE_HOLD_EXISTS`` or ``SEAT_UNAVAILABLE``), plus 401 from the
            authentication dependency.
    """
    return await service.hold(
        conn,
        user.id,
        payload.showtime_id,
        payload.seat_ids,
        datetime.now(UTC),
    )


@router.get("/reservations", response_model=Page[ReservationListItem])
async def list_reservations(
    user: Annotated[UserRecord, Depends(get_current_user)],
    conn: DbConnection,
    page: Annotated[PageParams, Depends(get_page_params)],
    status: ReservationStatus | None = None,
    upcoming: bool | None = None,
) -> Page[ReservationListItem]:
    """Return one page of the caller's reservations (SPEC-5 FR-1, FR-5).

    There is no ``user_id`` parameter: the service always filters by the
    authenticated user, so the endpoint cannot be pointed at someone else's
    reservations (FR-1). The literal path is declared before the
    ``/reservations/{reservation_id}`` route so the two never compete for a
    request.

    Args:
        user: The authenticated user, resolved by ``get_current_user``; only
            this user's reservations are selected.
        conn: The request-scoped connection; this route only reads.
        page: ``limit`` (1-100, default 20) and ``offset`` (>= 0) parsed by the
            shared dependency; out-of-range values are a 422 (FR-5).
        status: Optional effective-status filter. A ``held`` row past its expiry
            matches ``expired``; an unknown value is rejected by FastAPI as a
            422 before the handler runs (FR-3).
        upcoming: Optional showtime filter; ``True`` keeps showtimes starting
            after now, ``False`` keeps started ones, omitted keeps both (FR-4).

    Returns:
        Page[ReservationListItem]: The page's reservations, newest first, each
        with its seats, embedded showtime/movie/screen and computed
        ``total_cents``, plus the unpaginated ``total`` and the echoed window
        (FR-2, FR-5). A user with no reservations gets an empty page, not a 404.

    Raises:
        ApiError: 401 from the authentication dependency; nothing else -- a
        page with no matches is a normal 200 (FR-8).
    """
    # ``ListParams`` is the single declaration of this endpoint's query; the
    # handler only parses, never filters (SPEC-5 D5, Section 2.3).
    params = ListParams(status=status, upcoming=upcoming, page=page)
    items, total = await service.list_reservations(
        conn,
        user.id,
        params.status,
        params.upcoming,
        page.limit,
        page.offset,
        datetime.now(UTC),
    )
    return Page[ReservationListItem](
        items=items,
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/reservations/{reservation_id}", response_model=ReservationOut)
async def read_reservation(
    reservation_id: int,
    user: Annotated[UserRecord, Depends(get_current_user)],
    conn: DbConnection,
) -> ReservationOut:
    """Return one reservation owned by the authenticated user (FR-9).

    Args:
        reservation_id: The reservation's primary key, taken from the path.
        user: The authenticated user, resolved by ``get_current_user``; the
            service filters by this id.
        conn: The request-scoped connection; this route only reads.

    Returns:
        ReservationOut: The owned reservation with its effective status, seats
        ordered by row and number, and computed ``total_cents``.

    Raises:
        ApiError: ``RESERVATION_NOT_FOUND`` with status 404 when the reservation
        does not exist or belongs to another user (the two cases are
        indistinguishable by design); 401 from the authentication dependency.
    """
    return await service.get_reservation(
        conn,
        user.id,
        reservation_id,
        datetime.now(UTC),
    )


@router.post("/reservations/{reservation_id}/confirm", response_model=ReservationOut)
async def confirm_reservation(
    reservation_id: int,
    user: Annotated[UserRecord, Depends(get_current_user)],
    conn: DbConnection,
) -> ReservationOut:
    """Confirm one held reservation owned by the authenticated user (FR-7).

    Confirming an already confirmed reservation is idempotent: the service
    returns its current state with status 200. No payment occurs in this
    version; the service marks the seam where a real provider would plug in.

    Args:
        reservation_id: The reservation's primary key, taken from the path.
        user: The authenticated user, resolved by ``get_current_user``; the
            service filters by this id.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        ReservationOut: The reservation in its confirmed state, with its seats
        and computed ``total_cents``.

    Raises:
        ApiError: ``RESERVATION_NOT_FOUND`` (404) for a missing or foreign
        reservation; ``RESERVATION_NOT_HELD`` (409) for a cancelled or stored
        expired reservation; ``RESERVATION_EXPIRED`` (409) for a held
        reservation past its expiry; ``SHOWTIME_STARTED`` (409) when the
        showtime has started; 401 from the authentication dependency.
    """
    return await service.confirm(
        conn,
        user.id,
        reservation_id,
        datetime.now(UTC),
    )


@router.post("/reservations/{reservation_id}/release", response_model=ReservationOut)
async def release_reservation(
    reservation_id: int,
    user: Annotated[UserRecord, Depends(get_current_user)],
    conn: DbConnection,
) -> ReservationOut:
    """Release one held reservation owned by the authenticated user (FR-8).

    Args:
        reservation_id: The reservation's primary key, taken from the path.
        user: The authenticated user, resolved by ``get_current_user``; the
            service filters by this id.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        ReservationOut: The reservation in its cancelled state, with its seats
        and computed ``total_cents``.

    Raises:
        ApiError: ``RESERVATION_NOT_FOUND`` (404) for a missing or foreign
        reservation; ``RESERVATION_NOT_HELD`` (409) when it is not a held,
        unexpired reservation; 401 from the authentication dependency.
    """
    return await service.release(
        conn,
        user.id,
        reservation_id,
        datetime.now(UTC),
    )


@router.post("/reservations/{reservation_id}/cancel", response_model=ReservationOut)
async def cancel_reservation(
    reservation_id: int,
    user: Annotated[UserRecord, Depends(get_current_user)],
    conn: DbConnection,
) -> ReservationOut:
    """Cancel one confirmed reservation owned by the authenticated user (FR-6).

    Unlike confirm, cancel is not idempotent: a reservation that is already
    cancelled is refused with 409 ``RESERVATION_NOT_CONFIRMED`` rather than
    answered with 200, so a client can tell a real cancel from a stale retry
    (SPEC-5 D2). Releasing a *held* reservation stays on the SPEC-4 ``release``
    route, whose request and response shapes are unchanged (FR-12).

    Args:
        reservation_id: The reservation's primary key, taken from the path.
        user: The authenticated user, resolved by ``get_current_user``; the
            service filters by this id.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        ReservationOut: The reservation in its cancelled state, with its seats
        and computed ``total_cents``.

    Raises:
        ApiError: ``RESERVATION_NOT_FOUND`` (404) for a missing or foreign
        reservation -- indistinguishable by design (FR-8);
        ``RESERVATION_NOT_CONFIRMED`` (409) when the stored status is not
        ``confirmed`` (FR-7); ``SHOWTIME_STARTED`` (409) when the showtime has
        already started (FR-7); 401 from the authentication dependency. Every
        error keeps the standard body shape rendered by ``main.py`` (FR-12).
    """
    return await service.cancel(
        conn,
        user.id,
        reservation_id,
        datetime.now(UTC),
    )
