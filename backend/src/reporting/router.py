"""Thin HTTP handlers for the admin reporting endpoints (SPEC-6 Section 2.3).

Every handler here only parses its query, calls the matching function in
:mod:`src.reporting.service` and shapes the response; the date window, the
aggregates, the orderings and the totals live in the service and in
:mod:`src.reporting.rules` (``docs/steering/conventions.md`` and
``principles.md``: "Business rules never live inside a FastAPI route handler").

Routes, all behind :func:`~src.auth.dependencies.require_admin` (FR-1):

    GET /admin/reservations        paginated ``Page[AdminReservationItem]``
    GET /admin/reports/capacity    paginated ``Page[CapacityRow]``
    GET /admin/reports/revenue     ``RevenueOut``, grouped by movie or by day

The paths sit under ``/admin`` and carry the shared ``admin`` documentation
tag, as the endpoint table of SPEC-6 Section 2.3 shows. They cannot be confused
with the ``/reservations`` routes of SPEC-4 and SPEC-5, which answer the
caller's *own* reservations: those paths have no ``/admin`` segment and no
route declares a path parameter that could swallow it.

``response_model`` is declared on every route, so a response is serialised from
the declared schema and no field SPEC-6 Section 2.3 does not list can reach a
client. Each handler reads the query into the one parameter model of
:mod:`src.reporting.schemas` and hands that model to the service unchanged, so
the query shape has a single declaration and an unknown value, a bad date or an
out-of-range ``limit``/``offset`` is answered with FastAPI's normal 422 body
before the handler runs (FR-9, FR-12).

``now`` is read from the clock here, once per request, and passed to the
service, so the effective-status filter and the reported statuses of one
response agree with each other (FR-2). A date range the report cannot use is
raised by the service as :class:`~src.core.errors.ApiError` (422
``INVALID_DATE_RANGE``) and rendered by the handlers installed in ``main.py``;
no handler here builds an error response, which keeps the standard error shape
defined in exactly one place (FR-12).

Connections are requested through :data:`~src.db.engine.DbConnection`, the one
alias that carries ``scope="function"``, so a handler shares one connection
and one transaction with the authorization dependency (SPEC-2 Sections 2.5,
2.7). The reports only read: nothing here writes or commits (FR-8).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from src.auth.dependencies import require_admin
from src.auth.service import UserRecord
from src.core.enums import ReservationStatus
from src.core.pagination import Page, PageParams, get_page_params
from src.db.engine import DbConnection
from src.reporting import service
from src.reporting.schemas import (
    AdminReservationItem,
    AdminReservationListParams,
    CapacityListParams,
    CapacityRow,
    GroupBy,
    RevenueOut,
    RevenueParams,
)

# One router for the three SPEC-6 routes: all of them are admin-only, so the
# ``/admin`` prefix and the ``admin`` tag are written once, next to the other
# admin routers of the project. The authorization dependency is declared per
# route rather than on the router, because every handler needs the resolved
# admin's id for the SPEC-6 FR-7 log line.
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/reservations", response_model=Page[AdminReservationItem])
async def list_admin_reservations(
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
    page: Annotated[PageParams, Depends(get_page_params)],
    movie_id: int | None = None,
    showtime_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: ReservationStatus | None = None,
) -> Page[AdminReservationItem]:
    """Return one page of every user's reservations, newest first (FR-2).

    The caller must be an admin: ``require_admin`` answers 401 before anything
    else when the request carries no valid token and 403 when the account is
    not an administrator, so no query runs for either case (FR-1).

    Args:
        admin: The acting administrator, resolved by ``require_admin``; only its
            id is passed on, for the SPEC-6 FR-7 log line.
        conn: The request-scoped connection; this route only reads.
        page: ``limit`` (1-100, default 20) and ``offset`` (>= 0) parsed by the
            shared dependency; an out-of-range value is a 422 (FR-9).
        movie_id: Optional filter on the movie of the reservation's showtime. An
            id that matches nothing yields an empty page, not a 404.
        showtime_id: Optional filter on the reservation's showtime, likewise.
        date_from: Optional first inclusive local day of the showtime; ``None``
            leaves the lower side unbounded.
        date_to: Optional last inclusive local day of the showtime; ``None``
            leaves the upper side unbounded.
        status: Optional filter on the *effective* status, one of the four
            shared statuses; a value outside them is rejected by FastAPI with a
            422 before the handler runs (FR-12).

    Returns:
        Page[AdminReservationItem]: The page's reservations with owner, seats,
        showtime, movie, screen, effective status and ``total_cents``, plus the
        unpaginated ``total`` and the echoed window (FR-2).

    Raises:
        ApiError: 401 or 403 from the authorization dependency, and
            ``INVALID_DATE_RANGE`` with status 422 when ``date_from`` is after
            ``date_to`` (FR-3) -- the range rule itself lives in
            :func:`src.reporting.rules.validate_date_range`.
    """
    # ``AdminReservationListParams`` is the single declaration of this query;
    # the handler only parses, it never filters (SPEC-6 Section 2.3).
    params = AdminReservationListParams(
        movie_id=movie_id,
        showtime_id=showtime_id,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
    )
    items, total = await service.list_reservations_admin(
        conn,
        params,
        page.limit,
        page.offset,
        datetime.now(UTC),
        admin_user_id=admin.id,
    )
    return Page[AdminReservationItem](
        items=items,
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/reports/capacity", response_model=Page[CapacityRow])
async def read_capacity_report(
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
    page: Annotated[PageParams, Depends(get_page_params)],
    showtime_id: int | None = None,
    movie_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Page[CapacityRow]:
    """Return one page of per-showtime capacity (FR-4).

    The caller must be an admin: ``require_admin`` answers 401 before anything
    else when the request carries no valid token and 403 when the account is
    not an administrator, so no query runs for either case (FR-1).

    Args:
        admin: The acting administrator, resolved by ``require_admin``; only its
            id is passed on, for the SPEC-6 FR-7 log line.
        conn: The request-scoped connection; this route only reads.
        page: ``limit`` (1-100, default 20) and ``offset`` (>= 0) parsed by the
            shared dependency; an out-of-range value is a 422 (FR-9).
        showtime_id: Optional filter on one showtime. An id that matches nothing
            yields an empty page, not a 404.
        movie_id: Optional filter on the movie shown; likewise.
        date_from: Optional first inclusive local day of the showtime; ``None``
            leaves the lower side unbounded.
        date_to: Optional last inclusive local day of the showtime; ``None``
            leaves the upper side unbounded.

    Returns:
        Page[CapacityRow]: For each matching showtime ``total_seats``,
        ``sold_seats`` (confirmed only), ``held_seats`` (unexpired holds only)
        and ``available_seats = total - sold - held``, ordered by
        ``starts_at, id``, plus the unpaginated ``total`` (FR-4). A showtime
        with no reservations is included with all seats available.

    Raises:
        ApiError: 401 or 403 from the authorization dependency, and
            ``INVALID_DATE_RANGE`` with status 422 when ``date_from`` is after
            ``date_to`` (FR-3).
    """
    # ``CapacityListParams`` is the single declaration of this query; the
    # handler only parses, it never counts seats (SPEC-6 Section 2.3).
    params = CapacityListParams(
        showtime_id=showtime_id,
        movie_id=movie_id,
        date_from=date_from,
        date_to=date_to,
        page=page,
    )
    rows, total = await service.capacity_report(
        conn,
        params,
        page.limit,
        page.offset,
        datetime.now(UTC),
        admin_user_id=admin.id,
    )
    return Page[CapacityRow](
        items=rows,
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/reports/revenue", response_model=RevenueOut)
async def read_revenue_report(
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
    date_from: date | None = None,
    date_to: date | None = None,
    movie_id: int | None = None,
    group_by: GroupBy = "movie",
) -> RevenueOut:
    """Return revenue grouped by movie or by local day (FR-5, FR-6).

    The caller must be an admin: ``require_admin`` answers 401 before anything
    else when the request carries no valid token and 403 when the account is
    not an administrator, so no query runs for either case (FR-1).

    This route is not paginated: it returns one row per movie or per day, and
    ``group_by=day`` already bounds the row count to 366, so ``limit`` and
    ``offset`` play no part here (FR-9 covers the two list routes).

    Args:
        admin: The acting administrator, resolved by ``require_admin``; only its
            id is passed on, for the SPEC-6 FR-7 log line.
        conn: The request-scoped connection; this route only reads.
        date_from: Optional first inclusive local day of the showtime; ``None``
            leaves the lower side unbounded, unless ``group_by`` is ``day``.
        date_to: Optional last inclusive local day of the showtime; ``None``
            leaves the upper side unbounded, unless ``group_by`` is ``day``.
        movie_id: Optional restriction to one movie. An id that matches nothing
            yields zero totals and no rows, not a 404.
        group_by: ``movie`` (the default) or ``day``; any other value is
            rejected by FastAPI with a 422 before the handler runs (FR-12).

    Returns:
        RevenueOut: ``total_cents`` and ``tickets_sold`` with the rows they are
        summed from, the echoed range and the grouping (FR-6). Rows are ordered
        by ``revenue_cents`` descending for ``movie`` and by date for ``day``,
        so the totals and the rows can never disagree.

    Raises:
        ApiError: 401 or 403 from the authorization dependency, and
            ``INVALID_DATE_RANGE`` with status 422 when ``date_from`` is after
            ``date_to``, when ``group_by=day`` lacks either date, or when that
            range spans more than 366 days (FR-3, FR-6).
    """
    # ``RevenueParams`` is the single declaration of this query; the handler
    # only parses, it never aggregates (SPEC-6 Section 2.3).
    params = RevenueParams(
        date_from=date_from,
        date_to=date_to,
        movie_id=movie_id,
        group_by=group_by,
    )
    return await service.revenue_report(conn, params, admin_user_id=admin.id)
