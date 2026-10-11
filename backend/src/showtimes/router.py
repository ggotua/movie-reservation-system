"""Thin HTTP handlers for the screen and showtime endpoints (SPEC-3 Section 2.3).

Every handler only parses its inputs, calls the matching function in
:mod:`src.showtimes.service` and shapes the response; the rules themselves
(overlap, ``starts_at`` in the future, the browse window) live in the service
and in :mod:`src.showtimes.rules`, so the route layer stays thin and the rules
stay callable without a framework or a live request
(``docs/steering/conventions.md`` and ``principles.md``: "Business rules never
live inside a FastAPI route handler").

Two routers keep the public browsing apart from the admin writes, so the
generated documentation groups them and the admin ones are visibly the ones
behind :func:`~src.auth.dependencies.require_admin` (SPEC-3 Section 2.6):

    GET    /showtimes                      public_router, paginated
    GET    /showtimes/{showtime_id}        public_router
    GET    /admin/screens                  admin_router
    POST   /admin/screens                  admin_router, 201
    POST   /admin/showtimes                admin_router, 201
    DELETE /admin/showtimes/{showtime_id}  admin_router, 204

``response_model`` is declared on every route, so a response is serialised from
the declared schema -- and a showtime response never grows a seat-availability
field, which is SPEC-4's (SPEC-3 Section 2.3). Failures are raised by the
service as :class:`~src.core.errors.ApiError` and rendered by the handlers
installed in ``main.py``, so the error body shape stays defined in exactly one
place (FR-16). No handler here builds an error response itself.

The dates and instants this module works with (SPEC-3 FR-15, decision D2):

* the ``date`` query parameter is a plain calendar date, parsed by FastAPI from
  ``YYYY-MM-DD``; anything else is a normal request-validation failure (422,
  Section 2.4). Its meaning -- a day in ``CINEMA_TIMEZONE`` -- is applied by
  the service;
* ``now`` is read from the clock here, once per request, and passed to the
  service, so the service never reads the clock itself and a test can pin the
  boundary (SPEC-3 tests 31, 37, 41).

Connections are requested through :data:`~src.db.engine.DbConnection`, the one
alias that carries ``scope="function"``, so a write is committed before the
response is sent and the handler shares one connection and one transaction with
the authorization dependency (SPEC-2 Sections 2.5, 2.7).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status

from src.auth.dependencies import require_admin
from src.auth.service import UserRecord
from src.core.pagination import Page, PageParams, get_page_params
from src.db.engine import DbConnection
from src.showtimes import service
from src.showtimes.schemas import (
    ScreenCreate,
    ScreenOut,
    ShowtimeCreate,
    ShowtimeOut,
)
from src.showtimes.service import ScreenRecord, ShowtimeRecord

# The public browsing reads carry no prefix and no token; the admin writes sit
# under ``/admin``, exactly as the endpoint table of SPEC-3 Section 2.3 shows.
public_router = APIRouter(tags=["showtimes"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])


@public_router.get("/showtimes", response_model=Page[ShowtimeOut])
async def read_showtimes(
    conn: DbConnection,
    page: Annotated[PageParams, Depends(get_page_params)],
    date: date | None = None,
    movie_id: int | None = None,
) -> Page[ShowtimeOut]:
    """Return a page of showtimes for browsing (SPEC-3 Section 2.5, FR-13, FR-14).

    Args:
        conn: The request-scoped connection; this route only reads.
        page: The validated ``limit`` / ``offset`` pair produced by the shared
            dependency. An out-of-range value was already rejected with the
            standard 422 ``VALIDATION_ERROR`` body and is never clamped (FR-14).
        date: The calendar day to browse, understood in ``CINEMA_TIMEZONE``
            (decision D2). FastAPI parses it from ``YYYY-MM-DD`` and rejects
            anything else with 422 before this handler runs; the day is turned
            into its half-open UTC window by the service (FR-13).
        movie_id: When given, only showtimes of that movie are returned. An
            unknown movie id is not an error -- the page is simply empty
            (SPEC-3 Section 2.5, "Browse showtimes").

    Returns:
        Page[ShowtimeOut]: The page's showtimes, each with its movie and screen
        embedded, plus the total number of matching showtimes. A day with no
        showtimes yields an empty page with status 200, not a 404 (Section 3).
    """
    items, total = await service.list_showtimes(
        conn,
        day=date,
        movie_id=movie_id,
        limit=page.limit,
        offset=page.offset,
        now=datetime.now(UTC),
    )
    return Page[ShowtimeOut](
        items=[
            ShowtimeOut.model_validate(item, from_attributes=True) for item in items
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@public_router.get("/showtimes/{showtime_id}", response_model=ShowtimeOut)
async def read_showtime(showtime_id: int, conn: DbConnection) -> ShowtimeRecord:
    """Return one showtime with its movie and screen (SPEC-3 Section 2.3, FR-5).

    Args:
        showtime_id: The showtime's primary key, taken from the path.
        conn: The request-scoped connection; this route only reads.

    Returns:
        ShowtimeRecord: The showtime, with its movie and screen embedded from a
        single JOIN, serialised through ``ShowtimeOut``.

    Raises:
        ApiError: ``SHOWTIME_NOT_FOUND`` with status 404 when no showtime has
            that id (FR-5). The service raises it; ``main.py`` renders it.
    """
    return await service.get_showtime(conn, showtime_id)


@admin_router.get("/screens", response_model=list[ScreenOut])
async def read_screens(
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
) -> list[ScreenRecord]:
    """List every screen, ordered by name (SPEC-3 Section 2.3).

    Args:
        admin: The acting administrator, resolved by ``require_admin``. The list
            itself needs no admin data -- the parameter exists so that
            authentication (401) and authorization (403) are enforced before
            the handler runs, as FR-2 requires of every ``/admin/...`` route.
        conn: The request-scoped connection; this route only reads.

    Returns:
        list[ScreenRecord]: All screens, each with the ``seat_count`` derived
        from its dimensions, serialised through ``ScreenOut``. An empty list
        when no screen exists.
    """
    return await service.list_screens(conn)


@admin_router.post(
    "/screens",
    response_model=ScreenOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_screen(
    payload: ScreenCreate,
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
) -> ScreenRecord:
    """Create a screen together with its generated seats (SPEC-3 FR-9, D5).

    Args:
        payload: The validated request body. ``name`` is already stripped and
            bounded, ``rows`` is 1..26 and ``seats_per_row`` is 1..40, which is
            what keeps the generated labels within ``A``..``Z`` and ``1``..``40``.
        admin: The acting administrator, resolved by ``require_admin``; its id
            is used for the audit log line only.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        ScreenRecord: The created screen, with ``seat_count`` derived from its
        dimensions, serialised through ``ScreenOut``. All
        ``rows * seats_per_row`` seats exist by the time this returns; there is
        no seat editing in v1.

    Raises:
        ApiError: ``SCREEN_NAME_TAKEN`` with status 409 when a screen already
            has that name, in which case no seat is created (FR-9); plus
            401/403 from the dependency.
    """
    return await service.create_screen(conn, payload, admin.id)


@admin_router.post(
    "/showtimes",
    response_model=ShowtimeOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_showtime(
    payload: ShowtimeCreate,
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
) -> ShowtimeRecord:
    """Schedule a showtime (SPEC-3 Section 2.5, FR-10, FR-11).

    Args:
        payload: The validated request body. ``starts_at`` is already
            timezone-aware and normalised to UTC, and ``price_cents`` is
            already a strict integer in range; a naive datetime, a float price
            or an extra field was already rejected with 422 (FR-4, FR-10).
        admin: The acting administrator, resolved by ``require_admin``; its id
            is used for the audit log line only.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        ShowtimeRecord: The created showtime, with its movie and screen
        embedded, serialised through ``ShowtimeOut`` with ``starts_at`` in UTC
        (FR-15).

    Raises:
        ApiError: ``MOVIE_NOT_FOUND`` or ``SCREEN_NOT_FOUND`` with status 404
            when a referenced id does not exist (FR-5, decision D6);
            ``SHOWTIME_IN_PAST`` with status 422 when ``starts_at`` is not
            strictly in the future (FR-10); ``SHOWTIME_OVERLAP`` with status 409
            when another showtime on that screen starts less than
            ``SHOWTIME_SLOT_MINUTES`` away (FR-11); plus 401/403 from the
            dependency.
    """
    return await service.create_showtime(conn, payload, admin.id, datetime.now(UTC))


@admin_router.delete(
    "/showtimes/{showtime_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_showtime(
    showtime_id: int,
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
) -> None:
    """Delete a showtime that has not started and has no reservations (FR-12).

    Args:
        showtime_id: The showtime's primary key, taken from the path.
        admin: The acting administrator, resolved by ``require_admin``; its id
            is used for the audit log line only.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        None: The route answers 204 with an empty body. There is no showtime
        update in v1 (decision D4): changing a time or price means deleting the
        showtime and creating a new one.

    Raises:
        ApiError: ``SHOWTIME_NOT_FOUND`` with status 404 when no showtime has
            that id; ``SHOWTIME_STARTED`` with status 409 when it has already
            started; ``SHOWTIME_HAS_RESERVATIONS`` with status 409 when any
            reservation references it, whatever its status (FR-12, decision
            D4); plus 401/403 from the dependency.
    """
    await service.delete_showtime(conn, showtime_id, admin.id, datetime.now(UTC))
