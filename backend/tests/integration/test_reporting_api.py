"""Integration tests for the SPEC-6 admin reporting API (tests 5-28).

The three endpoints under test are ``GET /admin/reservations``,
``GET /admin/reports/capacity`` and ``GET /admin/reports/revenue`` (SPEC-6
Section 2.3). Every test creates its own administrator, movie, screen, showtime
and reservations inside the rolled-back connection that :mod:`tests.conftest`
shares with the application, so no test depends on seeded data and nothing is
ever committed.

The setup builders -- accounts, admin headers, screens, showtimes, holds,
confirmations, cancellations and the direct SQL inserts for states the API
cannot produce -- live in ``tests/conftest.py`` because the SPEC-4 and SPEC-5
suites need them as well; importing them keeps one definition of each builder,
and the cinema-zone date builders moved there with the SPEC-6 tests for the
same reason.

Groups: FR-1 is access (test 5); FR-2 and FR-9 are the admin reservation list
(tests 6-11); FR-4 is capacity (tests 12-15); FR-5, FR-6 and FR-7 are revenue
(tests 16-24); FR-8 and FR-10 are the safety checks (tests 25-27); FR-12 is
test 28. The concurrency test 29 belongs to a later prompt and is not written
here.

The clock is always the real one. Every instant is ``now`` plus an offset, the
timezone tests build Tbilisi wall-clock instants from ``CINEMA_TIMEZONE``
rather than from a hard-coded date, and the hold boundaries that must be exact
(tests 8 and 13) pass an explicit ``now`` to the service instead of racing the
request clock.

Every failure test asserts the status code, the error ``code`` and that no
reservation or seat row was written (SPEC-6 Section 2.4, FR-12).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncConnection

from src.core.config import get_settings
from src.core.enums import ReservationStatus
from src.core.pagination import PageParams
from src.reporting import service as reporting_service
from src.reporting.schemas import AdminReservationListParams, CapacityListParams
from tests.conftest import (
    _CANCELLED,
    _CONFIRMED,
    _EXPIRED,
    _HELD,
    _AdminHeaders,
    _assert_error,
    _assert_rows_unchanged,
    _AuthHeader,
    _cinema_zone,
    _confirm,
    _create_account,
    _CreateMovie,
    _CreateScreen,
    _CreateShowtime,
    _CreateUser,
    _error_payload,
    _FutureStart,
    _hold,
    _insert_reservation,
    _json_body,
    _local_day,
    _local_instant,
    _Login,
    _parse_optional_instant,
    _reservation_row_counts,
    _screen_seat_ids,
    _seat_ids,
    _set_reservation_created_at,
    _set_reservation_status,
    _stored_reservation_fields,
    _stored_reservation_status,
)

# The three reporting endpoints (SPEC-6 Section 2.3).
_ADMIN_RESERVATIONS_URL = "/admin/reservations"
_CAPACITY_URL = "/admin/reports/capacity"
_REVENUE_URL = "/admin/reports/revenue"

# The actor id passed to the service when a test calls it directly instead of
# going through a route. The service uses it for the audit log line only -- it is
# never written to a row -- so any positive integer is equally valid.
_REPORT_ACTOR_ID = 1

# The item fields SPEC-6 Section 2.3 lists for one admin reservation row. A
# response must carry exactly these, so a field the spec does not list (a
# password hash, say) can never reach a client (FR-11).
_ADMIN_ITEM_FIELDS = {
    "id",
    "status",
    "created_at",
    "expires_at",
    "confirmed_at",
    "cancelled_at",
    "seats",
    "total_cents",
    "user",
    "showtime",
}
_CAPACITY_ROW_FIELDS = {
    "showtime",
    "total_seats",
    "sold_seats",
    "held_seats",
    "available_seats",
}
_REVENUE_FIELDS = {
    "date_from",
    "date_to",
    "group_by",
    "total_cents",
    "tickets_sold",
    "rows",
}


async def _get(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Get one report route and return the raw response.

    Args:
        client: The ASGI client bound to the application under test.
        url: One of the three report URLs.
        headers: The caller's ``Authorization`` header, or ``None`` to send an
            unauthenticated request.
        params: Query parameters, or ``None`` to send no query string at all.

    Returns:
        httpx.Response: The unasserted response, so a test can report a failure
        with its body attached.
    """
    return await client.get(url, headers=headers, params=params)


def _page(response: httpx.Response) -> tuple[list[dict[str, Any]], int, int, int]:
    """Assert a paginated report response and return its page.

    Args:
        response: The raw response of a list route.

    Returns:
        tuple: The items, the unpaginated ``total``, the echoed ``limit`` and
        the echoed ``offset``.

    Raises:
        AssertionError: If the status is not 200, the body is not the SPEC-3
            envelope, or ``total`` / ``limit`` / ``offset`` is not an integer.
    """
    assert response.status_code == 200, response.text
    body = _json_body(response)
    assert set(body) == {"items", "total", "limit", "offset"}, body
    items = body["items"]
    assert isinstance(items, list), body
    for item in items:
        assert isinstance(item, dict), item
    for name in ("total", "limit", "offset"):
        assert type(body[name]) is int, (name, body)
    return (
        [dict(item) for item in items],
        int(body["total"]),
        int(body["limit"]),
        int(body["offset"]),
    )


def _revenue_body(response: httpx.Response) -> dict[str, Any]:
    """Assert a 200 revenue response and return its body.

    Args:
        response: The raw response of ``GET /admin/reports/revenue``.

    Returns:
        dict[str, Any]: The parsed body, whose keys are exactly the fields of
        :class:`~src.reporting.schemas.RevenueOut`.

    Raises:
        AssertionError: If the status is not 200, the body is not a JSON object
            or its keys are not the declared ones.
    """
    assert response.status_code == 200, response.text
    body = _json_body(response)
    assert set(body) == _REVENUE_FIELDS, body
    assert isinstance(body["rows"], list), body
    for row in body["rows"]:
        assert isinstance(row, dict), row
    return body


async def _ids_and_total(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> tuple[list[int], int]:
    """Return the reservation ids and ``total`` of one list request.

    Args:
        client: The ASGI client bound to the application under test.
        url: The list route, ``/admin/reservations`` or ``/reservations``.
        headers: The caller's ``Authorization`` header.
        params: Query parameters, or ``None`` to send none.

    Returns:
        tuple: The ids in page order and the unpaginated ``total``.
    """
    items, total, _limit, _offset = _page(await _get(client, url, headers, params))
    return [int(item["id"]) for item in items], total


def _row_of(items: list[dict[str, Any]], showtime_id: int) -> dict[str, Any]:
    """Return the capacity row of one showtime.

    Args:
        items: The page's capacity rows.
        showtime_id: The showtime to look for.

    Returns:
        dict[str, Any]: The matching row.

    Raises:
        AssertionError: If the page has no row for that showtime, with the rows
            it does contain attached.
    """
    for item in items:
        showtime = item["showtime"]
        assert isinstance(showtime, dict), item
        if int(showtime["id"]) == showtime_id:
            return item
    raise AssertionError(f"no capacity row for {showtime_id} in {items}")


def _capacity_showtime_ids(items: list[dict[str, Any]]) -> list[int]:
    """Return the showtime ids of a capacity page, in page order.

    Args:
        items: The page's capacity rows.

    Returns:
        list[int]: One showtime id per row, ordered as the page ordered them.
    """
    return [int(item["showtime"]["id"]) for item in items]


def _hold_window() -> timedelta:
    """Return the configured hold window (``HOLD_MINUTES``).

    Returns:
        timedelta: The length of a hold, taken from the same settings the
        service reads, so a test boundary and the rule always agree.
    """
    return timedelta(minutes=get_settings().hold_minutes)


def _instant(value: str) -> datetime:
    """Parse a serialised instant that the response must always carry.

    Args:
        value: The ISO 8601 instant from a response body.

    Returns:
        datetime: The parsed timezone-aware instant.

    Raises:
        AssertionError: If the value is missing, so a comparison never runs
            against ``None``.
    """
    parsed = _parse_optional_instant(value)
    assert parsed is not None, value
    return parsed


async def _confirmed_reservation(
    conn: AsyncConnection,
    *,
    user_id: int,
    showtime_id: int,
    seat_ids: list[int],
    created_at: datetime | None = None,
) -> int:
    """Insert one confirmed reservation and its seat rows directly.

    Revenue and capacity only read, so a confirmed reservation can be created
    with SQL instead of a hold plus a confirmation; the state it produces is the
    same one ``POST /reservations/{reservation_id}/confirm`` leaves behind.

    Args:
        conn: The test's connection.
        user_id: The owner's primary key.
        showtime_id: The showtime the reservation belongs to.
        seat_ids: The seats to mark confirmed.
        created_at: The creation instant, defaulting to now.

    Returns:
        int: The inserted reservation's primary key.
    """
    instant = created_at if created_at is not None else datetime.now(UTC)
    return await _insert_reservation(
        conn,
        user_id=user_id,
        showtime_id=showtime_id,
        seat_ids=seat_ids,
        status=_CONFIRMED,
        created_at=instant,
        confirmed_at=instant,
    )


async def _add_showtime(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    create_movie: _CreateMovie,
    create_screen: _CreateScreen,
    create_showtime: _CreateShowtime,
    *,
    title: str,
    screen_name: str,
    starts_at: datetime,
    price_cents: int,
    rows: int = 1,
    seats_per_row: int = 4,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Create a movie, a screen with seats and a showtime through the API.

    Used by the tests that need more than the one screening
    :func:`tests.conftest._create_screening` builds; the administrator is
    created once per test and reused for every call.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting administrator's ``Authorization`` header.
        create_movie: The shared movie fixture.
        create_screen: The shared screen fixture.
        create_showtime: The shared showtime fixture.
        title: The movie title.
        screen_name: The screen name, which must be unique.
        starts_at: The timezone-aware start instant, strictly in the future.
        price_cents: Ticket price in integer cents.
        rows: Number of seat rows.
        seats_per_row: Seats in each row.

    Returns:
        tuple: The movie, the screen and the showtime response bodies, in that
        order.
    """
    movie = await create_movie(client, headers, title)
    screen = await create_screen(client, headers, screen_name, rows, seats_per_row)
    showtime = await create_showtime(
        client,
        headers,
        int(movie["id"]),
        int(screen["id"]),
        starts_at,
        price_cents,
    )
    return movie, screen, showtime


# --- Access (FR-1, test 5) -------------------------------------------------


async def test_all_report_routes_return_401_anonymous_and_403_for_user(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """Every report route is admin-only: 401 without a token, 403 for a user.

    A real hold exists first, so the "nothing was written" check runs against
    non-empty tables and a rejected request has something it *could* have
    changed (FR-1, FR-8).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.access.admin@example.com"
    )
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Access Movie",
        screen_name="Spec6 Access Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_000,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, user_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec6.access.user@example.com",
    )
    held = await _hold(client, user_headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    before = await _reservation_row_counts(rollback_connection)

    for url in (_ADMIN_RESERVATIONS_URL, _CAPACITY_URL, _REVENUE_URL):
        _assert_error(await _get(client, url), 401, "UNAUTHENTICATED")
        _assert_error(await _get(client, url, user_headers), 403, "FORBIDDEN")

    await _assert_rows_unchanged(rollback_connection, before)


# --- Admin reservation list (FR-2, FR-9, tests 6-11) -----------------------


async def test_admin_reservations_lists_all_users_newest_first_with_user_and_showtime(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """The admin list returns every user's rows newest first with their refs.

    One confirmed reservation of two seats and one hold of one seat, owned by
    two different users on the same showtime: the page contains both, in
    ``created_at DESC, id DESC`` order, each with its owner (id and email only),
    its seats in map order, the showtime with its movie and screen, the
    effective status and ``total_cents`` (FR-2, FR-11, test 6).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.list.admin@example.com"
    )
    movie, screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 List Movie",
        screen_name="Spec6 List Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_500,
        rows=2,
        seats_per_row=2,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)

    user_a_id, a_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.list.a@example.com"
    )
    user_b_id, b_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.list.b@example.com"
    )
    hold_a = await _hold(client, a_headers, showtime_id, seat_ids[:2])
    assert hold_a.status_code == 201, hold_a.text
    reservation_a = int(_json_body(hold_a)["id"])
    confirmed = await _confirm(client, a_headers, reservation_a)
    assert confirmed.status_code == 200, confirmed.text
    hold_b = await _hold(client, b_headers, showtime_id, [seat_ids[2]])
    assert hold_b.status_code == 201, hold_b.text
    reservation_b = int(_json_body(hold_b)["id"])

    response = await _get(client, _ADMIN_RESERVATIONS_URL, headers)
    items, total, limit, offset = _page(response)

    assert total == 2, items
    assert (limit, offset) == (20, 0), (limit, offset)
    assert [int(item["id"]) for item in items] == [reservation_b, reservation_a]

    newest, oldest = items[0], items[1]
    for item in items:
        assert set(item) == _ADMIN_ITEM_FIELDS, item
        assert set(item["showtime"]) == {
            "id",
            "starts_at",
            "price_cents",
            "movie",
            "screen",
        }, item

    assert newest["status"] == _HELD
    assert newest["expires_at"] is not None
    assert newest["confirmed_at"] is None
    assert newest["cancelled_at"] is None
    assert newest["user"] == {"id": user_b_id, "email": "spec6.list.b@example.com"}
    assert newest["total_cents"] == 1_500
    assert [seat["seat_id"] for seat in newest["seats"]] == [seat_ids[2]]

    assert oldest["status"] == _CONFIRMED
    assert oldest["expires_at"] is None
    assert oldest["confirmed_at"] is not None
    assert oldest["user"] == {"id": user_a_id, "email": "spec6.list.a@example.com"}
    assert oldest["total_cents"] == 2 * 1_500
    assert [seat["seat_id"] for seat in oldest["seats"]] == seat_ids[:2]
    for seat in oldest["seats"]:
        assert set(seat) == {"seat_id", "row_label", "seat_number"}, seat

    assert _instant(str(newest["created_at"])) >= _instant(str(oldest["created_at"]))
    assert oldest["showtime"]["id"] == showtime_id
    assert oldest["showtime"]["price_cents"] == 1_500
    assert oldest["showtime"]["movie"] == {
        "id": int(movie["id"]),
        "title": "Spec6 List Movie",
    }
    assert oldest["showtime"]["screen"] == {
        "id": int(screen["id"]),
        "name": "Spec6 List Screen",
    }
    assert _instant(str(oldest["showtime"]["starts_at"])) == _instant(
        str(showtime["starts_at"])
    )
    assert "password" not in response.text.lower()


async def test_admin_reservations_filter_by_movie_showtime_and_status(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """The four filters select exactly the rows they describe (FR-2, test 7).

    Three reservations across two movies and two showtimes: one confirmed on
    the first showtime, one held and one cancelled on the second. Each filter
    is checked on its own and in combination, and a filter pair that matches
    nothing is an empty page with ``total`` 0 rather than an error.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.filter.admin@example.com"
    )
    movie_one, _screen_one, showtime_one = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Filter Movie One",
        screen_name="Spec6 Filter Screen One",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_000,
        seats_per_row=3,
    )
    movie_two, _screen_two, showtime_two = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Filter Movie Two",
        screen_name="Spec6 Filter Screen Two",
        starts_at=future_start(timedelta(days=2)),
        price_cents=2_000,
        seats_per_row=3,
    )
    first_id = int(showtime_one["id"])
    second_id = int(showtime_two["id"])
    first_seats = await _seat_ids(client, first_id)
    second_seats = await _seat_ids(client, second_id)

    _user_a, a_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.filter.a@example.com"
    )
    _user_b, b_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.filter.b@example.com"
    )
    confirmed_hold = await _hold(client, a_headers, first_id, first_seats[:2])
    assert confirmed_hold.status_code == 201, confirmed_hold.text
    confirmed_id = int(_json_body(confirmed_hold)["id"])
    assert (await _confirm(client, a_headers, confirmed_id)).status_code == 200
    held_hold = await _hold(client, a_headers, second_id, [second_seats[0]])
    assert held_hold.status_code == 201, held_hold.text
    held_id = int(_json_body(held_hold)["id"])
    cancelled_hold = await _hold(client, b_headers, second_id, [second_seats[1]])
    assert cancelled_hold.status_code == 201, cancelled_hold.text
    cancelled_id = int(_json_body(cancelled_hold)["id"])
    assert (await _confirm(client, b_headers, cancelled_id)).status_code == 200
    cancelled = await client.post(
        f"/reservations/{cancelled_id}/cancel", headers=b_headers
    )
    assert cancelled.status_code == 200, cancelled.text

    assert await _ids_and_total(client, _ADMIN_RESERVATIONS_URL, headers) == (
        [cancelled_id, held_id, confirmed_id],
        3,
    )
    assert await _ids_and_total(
        client, _ADMIN_RESERVATIONS_URL, headers, {"movie_id": int(movie_one["id"])}
    ) == ([confirmed_id], 1)
    assert await _ids_and_total(
        client, _ADMIN_RESERVATIONS_URL, headers, {"movie_id": int(movie_two["id"])}
    ) == ([cancelled_id, held_id], 2)
    assert await _ids_and_total(
        client, _ADMIN_RESERVATIONS_URL, headers, {"showtime_id": second_id}
    ) == ([cancelled_id, held_id], 2)
    assert await _ids_and_total(
        client, _ADMIN_RESERVATIONS_URL, headers, {"status": _CONFIRMED}
    ) == ([confirmed_id], 1)
    assert await _ids_and_total(
        client, _ADMIN_RESERVATIONS_URL, headers, {"status": _HELD}
    ) == ([held_id], 1)
    assert await _ids_and_total(
        client, _ADMIN_RESERVATIONS_URL, headers, {"status": _CANCELLED}
    ) == ([cancelled_id], 1)
    assert await _ids_and_total(
        client,
        _ADMIN_RESERVATIONS_URL,
        headers,
        {"movie_id": int(movie_two["id"]), "status": _CANCELLED},
    ) == ([cancelled_id], 1)
    assert await _ids_and_total(
        client,
        _ADMIN_RESERVATIONS_URL,
        headers,
        {"movie_id": int(movie_one["id"]), "showtime_id": second_id},
    ) == ([], 0)


async def test_admin_reservations_status_filter_uses_effective_status(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """A stale hold is reported and filtered as ``expired`` without a write.

    Four holds share one showtime: one clearly fresh, one clearly stale (both
    checked through the API) and one on each side of the exact boundary (checked
    by calling the service with an explicit ``now``, so the one-second
    difference is not a race). The stored rows must all still say ``held``
    afterwards, because reads never write (FR-2, FR-10, test 8).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.status.admin@example.com"
    )
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Status Movie",
        screen_name="Spec6 Status Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_000,
        seats_per_row=4,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)

    # One active hold per user and showtime, so each hold needs its own account.
    names = ("stale", "fresh", "active_boundary", "stale_boundary")
    user_headers_by_name: dict[str, dict[str, str]] = {}
    for name in names:
        _user_id, account_headers = await _create_account(
            client,
            create_user,
            login,
            auth_header,
            f"spec6.status.{name}@example.com",
        )
        user_headers_by_name[name] = account_headers

    reservation_ids: dict[str, int] = {}
    for name, seat_id in zip(names, seat_ids, strict=True):
        held = await _hold(client, user_headers_by_name[name], showtime_id, [seat_id])
        assert held.status_code == 201, held.text
        reservation_ids[name] = int(_json_body(held)["id"])

    now = datetime.now(UTC)
    hold_window = _hold_window()
    await _set_reservation_created_at(
        rollback_connection,
        reservation_ids["stale"],
        now - hold_window - timedelta(minutes=5),
    )
    await _set_reservation_created_at(
        rollback_connection,
        reservation_ids["active_boundary"],
        now - hold_window + timedelta(seconds=1),
    )
    await _set_reservation_created_at(
        rollback_connection,
        reservation_ids["stale_boundary"],
        now - hold_window,
    )

    async def served(
        status: ReservationStatus | None,
    ) -> dict[int, tuple[ReservationStatus, datetime | None]]:
        """Return ``id -> (status, expires_at)`` for one service call."""
        params = AdminReservationListParams(
            showtime_id=showtime_id,
            status=status,
            page=PageParams(limit=20, offset=0),
        )
        items, _total = await reporting_service.list_reservations_admin(
            rollback_connection,
            params,
            20,
            0,
            now,
            admin_user_id=_REPORT_ACTOR_ID,
        )
        return {item.id: (item.status, item.expires_at) for item in items}

    everything = await served(None)
    assert everything[reservation_ids["stale"]][0] == _EXPIRED
    assert everything[reservation_ids["stale"]][1] is None
    assert everything[reservation_ids["fresh"]][0] == _HELD
    assert everything[reservation_ids["fresh"]][1] is not None
    assert everything[reservation_ids["active_boundary"]][0] == _HELD
    assert everything[reservation_ids["stale_boundary"]][0] == _EXPIRED

    assert set(await served(_HELD)) == {
        reservation_ids["fresh"],
        reservation_ids["active_boundary"],
    }
    assert set(await served(_EXPIRED)) == {
        reservation_ids["stale"],
        reservation_ids["stale_boundary"],
    }

    held_page, held_total, _limit, _offset = _page(
        await _get(
            client,
            _ADMIN_RESERVATIONS_URL,
            headers,
            {"showtime_id": showtime_id, "status": _HELD},
        )
    )
    held_ids = {int(item["id"]) for item in held_page}
    assert reservation_ids["fresh"] in held_ids
    assert reservation_ids["stale"] not in held_ids
    assert held_total >= 1

    expired_page, _expired_total, _limit, _offset = _page(
        await _get(
            client,
            _ADMIN_RESERVATIONS_URL,
            headers,
            {"showtime_id": showtime_id, "status": _EXPIRED},
        )
    )
    expired_item = next(
        item for item in expired_page if int(item["id"]) == reservation_ids["stale"]
    )
    assert expired_item["status"] == _EXPIRED
    assert expired_item["expires_at"] is None

    for reservation_id in reservation_ids.values():
        assert (
            await _stored_reservation_status(rollback_connection, reservation_id)
            == _HELD
        )


async def test_admin_reservations_date_range_uses_showtime_date_in_cinema_timezone(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """The date range selects local calendar days, not UTC days (FR-3, test 9).

    Three showtimes sit on local 00:00, 23:30 and 23:59 of one Tbilisi day. The
    midnight one is the previous UTC day, so a UTC-based filter would put it on
    the wrong day; the report must list all three under the local date and none
    of them under the neighbouring local days.
    """
    zone = _cinema_zone()
    assert get_settings().cinema_timezone == "Asia/Tbilisi"
    day = _local_day(zone, 2)
    # The premise of this test: local midnight really is the previous UTC day.
    midnight_utc = _local_instant(zone, day, 0, 0)
    assert midnight_utc.date() == day - timedelta(days=1), midnight_utc

    headers = await admin_headers(
        client, rollback_connection, "spec6.tz.admin@example.com"
    )
    user_id, _user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.tz.user@example.com"
    )
    created: dict[str, int] = {}
    instants = {
        "midnight": midnight_utc,
        "late_evening": _local_instant(zone, day, 23, 30),
        "last_minute": _local_instant(zone, day, 23, 59),
    }
    for suffix, starts_at in instants.items():
        _movie, screen, showtime = await _add_showtime(
            client,
            headers,
            create_movie_via_api,
            create_screen_via_api,
            create_showtime_via_api,
            title=f"Spec6 TZ Movie {suffix}",
            screen_name=f"Spec6 TZ Screen {suffix}",
            starts_at=starts_at,
            price_cents=1_000,
        )
        showtime_id = int(showtime["id"])
        created[suffix] = showtime_id
        seat_ids = await _screen_seat_ids(rollback_connection, int(screen["id"]))
        await _confirmed_reservation(
            rollback_connection,
            user_id=user_id,
            showtime_id=showtime_id,
            seat_ids=[seat_ids[0]],
        )

    params = {"date_from": day.isoformat(), "date_to": day.isoformat()}
    items, total, _limit, _offset = _page(
        await _get(client, _ADMIN_RESERVATIONS_URL, headers, params)
    )
    assert total == 3, items
    assert {int(item["showtime"]["id"]) for item in items} == set(created.values())
    for item in items:
        starts_at = _instant(str(item["showtime"]["starts_at"]))
        assert starts_at.astimezone(zone).date() == day, item
    midnight_item = next(
        item for item in items if int(item["showtime"]["id"]) == created["midnight"]
    )
    assert _instant(str(midnight_item["showtime"]["starts_at"])).date() == (
        day - timedelta(days=1)
    )
    assert midnight_item["status"] == _CONFIRMED

    for offset in (-1, 1):
        neighbouring = (day + timedelta(days=offset)).isoformat()
        _items, neighbour_total, _limit, _offset = _page(
            await _get(
                client,
                _ADMIN_RESERVATIONS_URL,
                headers,
                {"date_from": neighbouring, "date_to": neighbouring},
            )
        )
        assert neighbour_total == 0, (offset, _items)


async def test_admin_reservations_pagination_total_and_bounds_return_422(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """``limit``/``offset`` page the list, ``total`` ignores the window (FR-9).

    Three holds on one showtime: ``total`` stays 3 for every page, the window
    only changes the items, the default page is ``limit`` 20 / ``offset`` 0 and
    the boundary values 1 and 100 are accepted. Out-of-range values are a 422
    that writes nothing.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.page.admin@example.com"
    )
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Page Movie",
        screen_name="Spec6 Page Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_000,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    reservation_ids: list[int] = []
    # One active hold per user and showtime, so each seat needs its own account.
    for index, seat_id in enumerate(seat_ids):
        _user_id, user_headers = await _create_account(
            client,
            create_user,
            login,
            auth_header,
            f"spec6.page.user{index}@example.com",
        )
        held = await _hold(client, user_headers, showtime_id, [seat_id])
        assert held.status_code == 201, held.text
        reservation_ids.append(int(_json_body(held)["id"]))

    expected = list(reversed(reservation_ids))
    first, total, limit, offset = _page(
        await _get(client, _ADMIN_RESERVATIONS_URL, headers, {"limit": 2, "offset": 0})
    )
    assert [int(item["id"]) for item in first] == expected[:2]
    assert (total, limit, offset) == (3, 2, 0)

    second, total, limit, offset = _page(
        await _get(client, _ADMIN_RESERVATIONS_URL, headers, {"limit": 2, "offset": 2})
    )
    assert [int(item["id"]) for item in second] == expected[2:]
    assert (total, limit, offset) == (3, 2, 2)

    smallest, total, limit, _offset = _page(
        await _get(client, _ADMIN_RESERVATIONS_URL, headers, {"limit": 1})
    )
    assert len(smallest) == 1
    assert (total, limit) == (3, 1)

    largest, total, limit, _offset = _page(
        await _get(client, _ADMIN_RESERVATIONS_URL, headers, {"limit": 100})
    )
    assert len(largest) == 3
    assert (total, limit) == (3, 100)

    default_items, total, limit, offset = _page(
        await _get(client, _ADMIN_RESERVATIONS_URL, headers)
    )
    assert len(default_items) == 3
    assert (total, limit, offset) == (3, 20, 0)

    before = await _reservation_row_counts(rollback_connection)
    for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        _assert_error(
            await _get(client, _ADMIN_RESERVATIONS_URL, headers, bad),
            422,
            "VALIDATION_ERROR",
        )
    await _assert_rows_unchanged(rollback_connection, before)


async def test_admin_reservations_invalid_date_range_returns_422(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """A reversed date range is a 422 ``INVALID_DATE_RANGE`` on every report.

    A one-day range (``date_from == date_to``) stays valid, so the rule rejects
    only the impossible order rather than a narrow window (FR-3, test 11).
    """
    zone = _cinema_zone()
    day = _local_day(zone, 2)
    headers = await admin_headers(
        client, rollback_connection, "spec6.range.admin@example.com"
    )
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Range Movie",
        screen_name="Spec6 Range Screen",
        starts_at=_local_instant(zone, day, 18, 0),
        price_cents=1_000,
        seats_per_row=2,
    )
    user_id, _user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.range.user@example.com"
    )
    seat_ids = await _seat_ids(client, int(showtime["id"]))
    await _confirmed_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=int(showtime["id"]),
        seat_ids=[seat_ids[0]],
    )
    before = await _reservation_row_counts(rollback_connection)

    reversed_params = {
        "date_from": (day + timedelta(days=1)).isoformat(),
        "date_to": day.isoformat(),
    }
    for url in (_ADMIN_RESERVATIONS_URL, _CAPACITY_URL, _REVENUE_URL):
        _assert_error(
            await _get(client, url, headers, reversed_params),
            422,
            "INVALID_DATE_RANGE",
        )

    single_day = {"date_from": day.isoformat(), "date_to": day.isoformat()}
    _items, total, _limit, _offset = _page(
        await _get(client, _ADMIN_RESERVATIONS_URL, headers, single_day)
    )
    assert total == 1
    await _assert_rows_unchanged(rollback_connection, before)


# --- Capacity (FR-4, tests 12-15) ------------------------------------------


async def test_capacity_counts_total_sold_held_and_available_per_showtime(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """One row per showtime with total, sold, held and available (FR-4).

    A six-seat screen holds two confirmed seats and one further unexpired hold,
    so sold is 2, held 1 and available 3; the row also carries the showtime,
    movie and screen references the admin has to read it with (test 12).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.capacity.admin@example.com"
    )
    movie, screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Capacity Movie",
        screen_name="Spec6 Capacity Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_500,
        rows=2,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_a, a_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.capacity.a@example.com"
    )
    _user_b, b_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.capacity.b@example.com"
    )
    sold = await _hold(client, a_headers, showtime_id, seat_ids[:2])
    assert sold.status_code == 201, sold.text
    assert (
        await _confirm(client, a_headers, int(_json_body(sold)["id"]))
    ).status_code == 200
    held = await _hold(client, b_headers, showtime_id, [seat_ids[2]])
    assert held.status_code == 201, held.text

    items, total, _limit, _offset = _page(
        await _get(client, _CAPACITY_URL, headers, {"showtime_id": showtime_id})
    )

    assert total == 1, items
    row = _row_of(items, showtime_id)
    assert set(row) == _CAPACITY_ROW_FIELDS, row
    assert row["total_seats"] == 6
    assert row["sold_seats"] == 2
    assert row["held_seats"] == 1
    assert row["available_seats"] == 3
    assert row["showtime"]["id"] == showtime_id
    assert row["showtime"]["price_cents"] == 1_500
    assert _instant(str(row["showtime"]["starts_at"])) == _instant(
        str(showtime["starts_at"])
    )
    assert row["showtime"]["movie"] == {
        "id": int(movie["id"]),
        "title": "Spec6 Capacity Movie",
    }
    assert row["showtime"]["screen"] == {
        "id": int(screen["id"]),
        "name": "Spec6 Capacity Screen",
    }


async def test_capacity_ignores_cancelled_and_expired_and_stale_holds(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """Only confirmed seats and unexpired holds count towards occupancy (FR-4).

    A cancelled confirmed reservation, a cancelled hold, a stale hold and a
    fresh hold share one six-seat screen: through the API only the fresh hold is
    held. Two further holds then straddle the boundary exactly (age ``HOLD`` is
    stale, ``HOLD - 1 s`` is active) and the service is asked with an explicit
    ``now``, so the second is the only one of the pair that counts (test 13).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.capacity.stale.admin@example.com"
    )
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Capacity Stale Movie",
        screen_name="Spec6 Capacity Stale Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_000,
        rows=2,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    user_id, user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.capacity.stale@example.com"
    )
    _boundary_id, boundary_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.capacity.boundary@example.com"
    )
    _stale_boundary_id, stale_boundary_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec6.capacity.stale.boundary@example.com",
    )
    hold_window = _hold_window()

    cancelled_hold = await _hold(client, user_headers, showtime_id, [seat_ids[0]])
    assert cancelled_hold.status_code == 201, cancelled_hold.text
    cancelled_id = int(_json_body(cancelled_hold)["id"])
    assert (await _confirm(client, user_headers, cancelled_id)).status_code == 200
    cancelled = await client.post(
        f"/reservations/{cancelled_id}/cancel", headers=user_headers
    )
    assert cancelled.status_code == 200, cancelled.text

    former_hold = await _hold(client, user_headers, showtime_id, [seat_ids[1]])
    assert former_hold.status_code == 201, former_hold.text
    former_id = int(_json_body(former_hold)["id"])
    await _set_reservation_status(rollback_connection, former_id, _CANCELLED)

    # Inserted directly: a stale hold can no longer be produced through the API.
    await _insert_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=showtime_id,
        seat_ids=[seat_ids[2]],
        status=_HELD,
        created_at=datetime.now(UTC) - hold_window - timedelta(minutes=1),
    )

    fresh = await _hold(client, user_headers, showtime_id, [seat_ids[3]])
    assert fresh.status_code == 201, fresh.text

    items, total, _limit, _offset = _page(
        await _get(client, _CAPACITY_URL, headers, {"showtime_id": showtime_id})
    )
    assert total == 1
    row = _row_of(items, showtime_id)
    assert row["total_seats"] == 6
    assert row["sold_seats"] == 0
    assert row["held_seats"] == 1
    assert row["available_seats"] == 5

    active_boundary = await _hold(client, boundary_headers, showtime_id, [seat_ids[4]])
    assert active_boundary.status_code == 201, active_boundary.text
    stale_boundary = await _hold(
        client, stale_boundary_headers, showtime_id, [seat_ids[5]]
    )
    assert stale_boundary.status_code == 201, stale_boundary.text

    now = datetime.now(UTC)
    await _set_reservation_created_at(
        rollback_connection,
        int(_json_body(active_boundary)["id"]),
        now - hold_window + timedelta(seconds=1),
    )
    await _set_reservation_created_at(
        rollback_connection,
        int(_json_body(stale_boundary)["id"]),
        now - hold_window,
    )

    service_rows, service_total = await reporting_service.capacity_report(
        rollback_connection,
        CapacityListParams(
            showtime_id=showtime_id, page=PageParams(limit=20, offset=0)
        ),
        20,
        0,
        now,
        admin_user_id=_REPORT_ACTOR_ID,
    )
    assert service_total == 1
    (service_row,) = service_rows
    assert service_row.held_seats == 2
    assert service_row.sold_seats == 0


async def test_capacity_filters_order_and_pagination(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
) -> None:
    """Capacity is ordered ``starts_at, id`` and honours every filter (FR-4).

    Three showtimes: movie A on two different local days and movie B between
    them. Without reservations every row is fully available, which also proves
    showtimes nobody booked are listed. Movie, showtime and local-date filters
    narrow the page, unknown ids are an empty 200 page, and pagination plus the
    422 bounds behave as on the admin list (test 14).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.capacity.filter.admin@example.com"
    )
    zone = _cinema_zone()
    day = _local_day(zone, 3)
    movie_a = await create_movie_via_api(client, headers, "Spec6 Cap Filter Movie A")
    movie_b = await create_movie_via_api(client, headers, "Spec6 Cap Filter Movie B")
    screen_a = await create_screen_via_api(
        client, headers, "Spec6 Cap Filter Screen A", 1, 2
    )
    screen_b = await create_screen_via_api(
        client, headers, "Spec6 Cap Filter Screen B", 1, 2
    )
    screen_c = await create_screen_via_api(
        client, headers, "Spec6 Cap Filter Screen C", 1, 2
    )
    showtime_ids = {}
    for name, movie, screen, moment in (
        ("a_day_one", movie_a, screen_a, _local_instant(zone, day, 10, 0)),
        (
            "a_day_two",
            movie_a,
            screen_b,
            _local_instant(zone, day + timedelta(days=1), 10, 0),
        ),
        ("b_day_one", movie_b, screen_c, _local_instant(zone, day, 12, 0)),
    ):
        showtime = await create_showtime_via_api(
            client,
            headers,
            int(movie["id"]),
            int(screen["id"]),
            moment,
            1_000,
        )
        showtime_ids[name] = int(showtime["id"])

    items, total, _limit, _offset = _page(await _get(client, _CAPACITY_URL, headers))
    assert total == 3
    assert _capacity_showtime_ids(items) == [
        showtime_ids["a_day_one"],
        showtime_ids["b_day_one"],
        showtime_ids["a_day_two"],
    ]
    for item in items:
        assert set(item) == _CAPACITY_ROW_FIELDS, item
        assert item["total_seats"] == 2
        assert item["sold_seats"] == 0
        assert item["held_seats"] == 0
        assert item["available_seats"] == 2

    movie_items, total, _limit, _offset = _page(
        await _get(client, _CAPACITY_URL, headers, {"movie_id": int(movie_a["id"])})
    )
    assert total == 2
    assert _capacity_showtime_ids(movie_items) == [
        showtime_ids["a_day_one"],
        showtime_ids["a_day_two"],
    ]

    showtime_items, total, _limit, _offset = _page(
        await _get(
            client,
            _CAPACITY_URL,
            headers,
            {"showtime_id": showtime_ids["b_day_one"]},
        )
    )
    assert total == 1
    assert _capacity_showtime_ids(showtime_items) == [showtime_ids["b_day_one"]]

    for offset_days, expected in (
        (0, ["a_day_one", "b_day_one"]),
        (1, ["a_day_two"]),
    ):
        wanted = (day + timedelta(days=offset_days)).isoformat()
        day_items, total, _limit, _offset = _page(
            await _get(
                client,
                _CAPACITY_URL,
                headers,
                {"date_from": wanted, "date_to": wanted},
            )
        )
        assert total == len(expected)
        assert _capacity_showtime_ids(day_items) == [
            showtime_ids[name] for name in expected
        ]

    for params in ({"movie_id": 987_654}, {"showtime_id": 987_654}):
        unknown_items, total, _limit, _offset = _page(
            await _get(client, _CAPACITY_URL, headers, params)
        )
        assert unknown_items == []
        assert total == 0

    page_two, total, limit, offset = _page(
        await _get(client, _CAPACITY_URL, headers, {"limit": 1, "offset": 1})
    )
    assert _capacity_showtime_ids(page_two) == [showtime_ids["b_day_one"]]
    assert (total, limit, offset) == (3, 1, 1)

    before = await _reservation_row_counts(rollback_connection)
    for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        _assert_error(
            await _get(client, _CAPACITY_URL, headers, bad),
            422,
            "VALIDATION_ERROR",
        )
    await _assert_rows_unchanged(rollback_connection, before)


async def test_capacity_showtime_without_reservations_is_fully_available(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """A showtime nobody booked reports every seat available (FR-4, test 15).

    Two showtimes share one eight-seat screen; only the second has a confirmed
    seat. Both are listed -- the empty one with ``sold`` and ``held`` at zero --
    which is what an administrator needs to notice free capacity.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.capacity.empty.admin@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec6 Capacity Empty Movie")
    screen = await create_screen_via_api(
        client, headers, "Spec6 Capacity Empty Screen", 2, 4
    )
    empty_showtime = await create_showtime_via_api(
        client,
        headers,
        int(movie["id"]),
        int(screen["id"]),
        future_start(timedelta(days=1)),
        1_000,
    )
    booked_showtime = await create_showtime_via_api(
        client,
        headers,
        int(movie["id"]),
        int(screen["id"]),
        future_start(timedelta(days=2)),
        1_000,
    )
    empty_id = int(empty_showtime["id"])
    booked_id = int(booked_showtime["id"])
    user_id, _user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.capacity.empty@example.com"
    )
    seat_ids = await _screen_seat_ids(rollback_connection, int(screen["id"]))
    await _confirmed_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=booked_id,
        seat_ids=[seat_ids[0]],
    )

    items, total, _limit, _offset = _page(
        await _get(client, _CAPACITY_URL, headers, {"showtime_id": empty_id})
    )
    assert total == 1
    empty_row = _row_of(items, empty_id)
    assert empty_row["total_seats"] == 8
    assert empty_row["sold_seats"] == 0
    assert empty_row["held_seats"] == 0
    assert empty_row["available_seats"] == 8

    items, total, _limit, _offset = _page(
        await _get(client, _CAPACITY_URL, headers, {"showtime_id": booked_id})
    )
    assert total == 1
    booked_row = _row_of(items, booked_id)
    assert booked_row["sold_seats"] == 1
    assert booked_row["available_seats"] == 7

    items, total, _limit, _offset = _page(await _get(client, _CAPACITY_URL, headers))
    assert total == 2
    assert sorted(_capacity_showtime_ids(items)) == sorted([empty_id, booked_id])

    before = await _reservation_row_counts(rollback_connection)
    _items, _total, _limit, _offset = _page(await _get(client, _CAPACITY_URL, headers))
    await _assert_rows_unchanged(rollback_connection, before)


# --- Revenue (FR-5, tests 16-25) -------------------------------------------


async def test_revenue_counts_only_confirmed_seats_times_price(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """Revenue is confirmed seats times their showtime's price (FR-5, test 16).

    Movie A sells two seats at 1500 and movie B one seat at 2500. Each row is
    ``tickets_sold`` and ``revenue_cents`` for its movie, the response totals are
    their sums, and with no range requested both echoed dates are ``None`` and
    the grouping defaults to ``movie``.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.revenue.admin@example.com"
    )
    movie_a, _, showtime_a = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Revenue Movie A",
        screen_name="Spec6 Revenue Screen A",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_500,
    )
    movie_b, _, showtime_b = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Revenue Movie B",
        screen_name="Spec6 Revenue Screen B",
        starts_at=future_start(timedelta(days=2)),
        price_cents=2_500,
    )
    user_id, _user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.revenue.user@example.com"
    )
    seats_a = await _seat_ids(client, int(showtime_a["id"]))
    seats_b = await _seat_ids(client, int(showtime_b["id"]))
    await _confirmed_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=int(showtime_a["id"]),
        seat_ids=seats_a[:2],
    )
    await _confirmed_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=int(showtime_b["id"]),
        seat_ids=[seats_b[0]],
    )

    body = _revenue_body(await _get(client, _REVENUE_URL, headers))

    assert body["date_from"] is None
    assert body["date_to"] is None
    assert body["group_by"] == "movie"
    assert body["total_cents"] == 5_500
    assert body["tickets_sold"] == 3
    rows = [dict(row) for row in body["rows"]]
    assert rows == [
        {
            "movie_id": int(movie_a["id"]),
            "title": "Spec6 Revenue Movie A",
            "tickets_sold": 2,
            "revenue_cents": 3_000,
        },
        {
            "movie_id": int(movie_b["id"]),
            "title": "Spec6 Revenue Movie B",
            "tickets_sold": 1,
            "revenue_cents": 2_500,
        },
    ]
    for row in rows:
        assert set(row) == {"movie_id", "title", "tickets_sold", "revenue_cents"}
        unit_price = 1_500 if row["movie_id"] == int(movie_a["id"]) else 2_500
        assert row["revenue_cents"] == row["tickets_sold"] * unit_price


async def test_revenue_excludes_held_cancelled_and_expired(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """Only stored ``confirmed`` seats earn revenue (FR-5, test 17).

    One six-seat showtime at 1000 holds two confirmed seats, one fresh hold, one
    cancelled-after-confirmation reservation, one cancellation written straight
    to the table and one stale hold the admin list would call expired. Only the
    two confirmed seats are money.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.revenue.states.admin@example.com"
    )
    movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Revenue States Movie",
        screen_name="Spec6 Revenue States Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_000,
        rows=2,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    user_id, user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.revenue.states@example.com"
    )

    confirmed = await _hold(client, user_headers, showtime_id, seat_ids[:2])
    assert confirmed.status_code == 201, confirmed.text
    confirmed_id = int(_json_body(confirmed)["id"])
    assert (await _confirm(client, user_headers, confirmed_id)).status_code == 200

    cancelled = await _hold(client, user_headers, showtime_id, [seat_ids[3]])
    assert cancelled.status_code == 201, cancelled.text
    cancelled_id = int(_json_body(cancelled)["id"])
    assert (await _confirm(client, user_headers, cancelled_id)).status_code == 200
    assert (
        await client.post(f"/reservations/{cancelled_id}/cancel", headers=user_headers)
    ).status_code == 200

    former = await _hold(client, user_headers, showtime_id, [seat_ids[4]])
    assert former.status_code == 201, former.text
    await _set_reservation_status(
        rollback_connection, int(_json_body(former)["id"]), _CANCELLED
    )

    # A user may hold only one reservation per showtime, so the fresh hold comes
    # once every earlier reservation of this user has left the ``held`` state.
    fresh_hold = await _hold(client, user_headers, showtime_id, [seat_ids[2]])
    assert fresh_hold.status_code == 201, fresh_hold.text

    # The stale row is inserted directly: it models a hold whose window ran out,
    # which the API can no longer produce.
    await _insert_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=showtime_id,
        seat_ids=[seat_ids[5]],
        status=_HELD,
        created_at=datetime.now(UTC) - _hold_window() - timedelta(minutes=1),
    )

    body = _revenue_body(await _get(client, _REVENUE_URL, headers))

    assert body["total_cents"] == 2_000
    assert body["tickets_sold"] == 2
    assert body["rows"] == [
        {
            "movie_id": int(movie["id"]),
            "title": "Spec6 Revenue States Movie",
            "tickets_sold": 2,
            "revenue_cents": 2_000,
        }
    ]


async def test_revenue_by_movie_ordered_by_revenue_desc_and_total_matches_rows(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """Rows are ordered by revenue, and the totals are their sums (FR-5, FR-7).

    Three movies earn 2000, 6000 and nothing. The page lists the two earners in
    ``revenue_cents DESC, movie_id`` order, the third has no row at all, and
    ``total_cents`` / ``tickets_sold`` equal the sums of the rows the client
    receives -- not a separately computed number (test 18).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.revenue.order.admin@example.com"
    )
    user_id, _user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.revenue.order@example.com"
    )
    movies: dict[str, int] = {}
    for name, price_cents, sold_seats in (
        ("small", 1_000, 2),
        ("large", 2_000, 3),
        ("unsold", 5_000, 0),
    ):
        movie, _screen, showtime = await _add_showtime(
            client,
            headers,
            create_movie_via_api,
            create_screen_via_api,
            create_showtime_via_api,
            title=f"Spec6 Revenue Order {name}",
            screen_name=f"Spec6 Revenue Order Screen {name}",
            starts_at=future_start(timedelta(days=1)),
            price_cents=price_cents,
            rows=2,
            seats_per_row=2,
        )
        movies[name] = int(movie["id"])
        if sold_seats == 0:
            # No reservation at all, so the movie never reaches the report.
            continue
        seat_ids = await _seat_ids(client, int(showtime["id"]))
        await _confirmed_reservation(
            rollback_connection,
            user_id=user_id,
            showtime_id=int(showtime["id"]),
            seat_ids=seat_ids[:sold_seats],
        )

    body = _revenue_body(await _get(client, _REVENUE_URL, headers))
    rows = [dict(row) for row in body["rows"]]

    assert [row["movie_id"] for row in rows] == [movies["large"], movies["small"]]
    assert rows == [
        {
            "movie_id": movies["large"],
            "title": "Spec6 Revenue Order large",
            "tickets_sold": 3,
            "revenue_cents": 6_000,
        },
        {
            "movie_id": movies["small"],
            "title": "Spec6 Revenue Order small",
            "tickets_sold": 2,
            "revenue_cents": 2_000,
        },
    ]
    assert body["total_cents"] == sum(int(row["revenue_cents"]) for row in rows)
    assert body["tickets_sold"] == sum(int(row["tickets_sold"]) for row in rows)
    assert (body["total_cents"], body["tickets_sold"]) == (8_000, 5)
    assert movies["unsold"] not in {row["movie_id"] for row in rows}

    unsold_body = _revenue_body(
        await _get(client, _REVENUE_URL, headers, {"movie_id": movies["unsold"]})
    )
    assert unsold_body["rows"] == []
    assert (unsold_body["total_cents"], unsold_body["tickets_sold"]) == (0, 0)


async def test_revenue_by_day_uses_cinema_timezone(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """``group_by=day`` groups by the local day of the showtime (FR-6, test 19).

    Two showtimes at local 23:30 of one Tbilisi day and 00:30 of the next are
    both on the *same* UTC day (19:30 and 20:30). Grouping by UTC would merge
    them into one row; the report must return two rows, one per local day, with
    one confirmed seat each.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.revenue.day.admin@example.com"
    )
    zone = _cinema_zone()
    day = _local_day(zone, 2)
    first = _local_instant(zone, day, 23, 30)
    second = _local_instant(zone, day + timedelta(days=1), 0, 30)
    # The premise: both instants fall on the same UTC date.
    assert first.date() == second.date(), (first, second)

    _movie, _screen, showtime_one = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Revenue Day Movie",
        screen_name="Spec6 Revenue Day Screen One",
        starts_at=first,
        price_cents=1_200,
    )
    user_id, _user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.revenue.day@example.com"
    )
    _movie, _screen_two, showtime_two = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Revenue Day Movie Two",
        screen_name="Spec6 Revenue Day Screen Two",
        starts_at=second,
        price_cents=1_200,
    )
    for showtime in (showtime_one, showtime_two):
        seat_ids = await _seat_ids(client, int(showtime["id"]))
        await _confirmed_reservation(
            rollback_connection,
            user_id=user_id,
            showtime_id=int(showtime["id"]),
            seat_ids=[seat_ids[0]],
        )

    params = {
        "group_by": "day",
        "date_from": day.isoformat(),
        "date_to": (day + timedelta(days=1)).isoformat(),
    }
    body = _revenue_body(await _get(client, _REVENUE_URL, headers, params))

    assert body["group_by"] == "day"
    assert body["date_from"] == day.isoformat()
    assert body["date_to"] == (day + timedelta(days=1)).isoformat()
    assert body["rows"] == [
        {"date": day.isoformat(), "tickets_sold": 1, "revenue_cents": 1_200},
        {
            "date": (day + timedelta(days=1)).isoformat(),
            "tickets_sold": 1,
            "revenue_cents": 1_200,
        },
    ]
    assert (body["total_cents"], body["tickets_sold"]) == (2_400, 2)

    first_day_only = {
        "group_by": "day",
        "date_from": day.isoformat(),
        "date_to": day.isoformat(),
    }
    body = _revenue_body(await _get(client, _REVENUE_URL, headers, first_day_only))
    assert body["rows"] == [
        {"date": day.isoformat(), "tickets_sold": 1, "revenue_cents": 1_200}
    ]
    assert body["total_cents"] == 1_200


async def test_revenue_date_range_boundaries_are_inclusive_and_exclusive_as_specified(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """A local day includes its first and last minute, excludes the next (FR-6).

    One movie has a confirmed seat at 23:59 of the previous local day, 00:00 and
    23:59 of the reported day and 00:00 of the following one. Asking for the
    single day yields exactly 2000 -- the two boundary showtimes of that day --
    while asking for either neighbouring day still finds its own 1000, so the
    exclusion is the range's doing and not missing data (test 20).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.revenue.bounds.admin@example.com"
    )
    zone = _cinema_zone()
    day = _local_day(zone, 2)
    movie = await create_movie_via_api(client, headers, "Spec6 Revenue Bounds Movie")
    user_id, _user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.revenue.bounds@example.com"
    )
    moments = {
        "yesterday": _local_instant(zone, day - timedelta(days=1), 23, 59),
        "first_minute": _local_instant(zone, day, 0, 0),
        "last_minute": _local_instant(zone, day, 23, 59),
        "tomorrow": _local_instant(zone, day + timedelta(days=1), 0, 0),
    }
    for index, starts_at in enumerate(moments.values()):
        screen = await create_screen_via_api(
            client, headers, f"Spec6 Revenue Bounds Screen {index}", 1, 2
        )
        showtime = await create_showtime_via_api(
            client,
            headers,
            int(movie["id"]),
            int(screen["id"]),
            starts_at,
            1_000,
        )
        seat_ids = await _seat_ids(client, int(showtime["id"]))
        await _confirmed_reservation(
            rollback_connection,
            user_id=user_id,
            showtime_id=int(showtime["id"]),
            seat_ids=[seat_ids[0]],
        )

    for offset_days, expected_cents in ((-1, 1_000), (0, 2_000), (1, 1_000)):
        wanted = (day + timedelta(days=offset_days)).isoformat()
        body = _revenue_body(
            await _get(
                client,
                _REVENUE_URL,
                headers,
                {"date_from": wanted, "date_to": wanted},
            )
        )
        assert body["date_from"] == wanted
        assert body["date_to"] == wanted
        assert body["total_cents"] == expected_cents
        assert body["tickets_sold"] == expected_cents // 1_000

    day_params = {
        "group_by": "day",
        "date_from": day.isoformat(),
        "date_to": day.isoformat(),
    }
    body = _revenue_body(await _get(client, _REVENUE_URL, headers, day_params))
    assert body["rows"] == [
        {"date": day.isoformat(), "tickets_sold": 2, "revenue_cents": 2_000}
    ]
    assert body["total_cents"] == 2_000


async def test_revenue_group_by_day_requires_range_and_max_366_days(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """``group_by=day`` needs both dates and at most 366 of them (FR-6, test 21).

    A daily report over an unbounded range is refused with
    ``INVALID_DATE_RANGE``, as is a one-sided range, and so is a 367-day window;
    exactly 366 days is accepted. ``group_by=movie`` needs no dates at all, and
    none of the refusals writes a row.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.revenue.limit.admin@example.com"
    )
    zone = _cinema_zone()
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Revenue Limit Movie",
        screen_name="Spec6 Revenue Limit Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_000,
    )
    user_id, _user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.revenue.limit@example.com"
    )
    seat_ids = await _seat_ids(client, int(showtime["id"]))
    await _confirmed_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=int(showtime["id"]),
        seat_ids=[seat_ids[0]],
    )
    first_day = _instant(str(showtime["starts_at"])).astimezone(zone).date()
    before = await _reservation_row_counts(rollback_connection)

    for params in (
        {"group_by": "day"},
        {"group_by": "day", "date_from": first_day.isoformat()},
        {"group_by": "day", "date_to": first_day.isoformat()},
        {
            "group_by": "day",
            "date_from": first_day.isoformat(),
            "date_to": (first_day + timedelta(days=366)).isoformat(),
        },
    ):
        _assert_error(
            await _get(client, _REVENUE_URL, headers, params),
            422,
            "INVALID_DATE_RANGE",
        )

    longest_allowed = {
        "group_by": "day",
        "date_from": first_day.isoformat(),
        "date_to": (first_day + timedelta(days=365)).isoformat(),
    }
    body = _revenue_body(await _get(client, _REVENUE_URL, headers, longest_allowed))
    assert body["group_by"] == "day"
    assert body["total_cents"] == 1_000
    assert body["rows"] == [
        {
            "date": first_day.isoformat(),
            "tickets_sold": 1,
            "revenue_cents": 1_000,
        }
    ]

    movie_body = _revenue_body(
        await _get(client, _REVENUE_URL, headers, {"group_by": "movie"})
    )
    assert movie_body["total_cents"] == 1_000
    await _assert_rows_unchanged(rollback_connection, before)


async def test_revenue_empty_range_returns_zero_totals_and_no_rows(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """A range with no confirmed seats is a 200 with zero totals (FR-7, test 22).

    A confirmed seat exists outside the requested days, and a day with no
    showtime is asked for as well: both answers carry the echoed request, zero
    ``total_cents``/``tickets_sold`` and no rows -- never a 404 and never a
    missing field.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.revenue.empty.admin@example.com"
    )
    zone = _cinema_zone()
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Revenue Empty Movie",
        screen_name="Spec6 Revenue Empty Screen",
        starts_at=future_start(timedelta(days=3)),
        price_cents=1_000,
    )
    user_id, _user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.revenue.empty@example.com"
    )
    seat_ids = await _seat_ids(client, int(showtime["id"]))
    await _confirmed_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=int(showtime["id"]),
        seat_ids=[seat_ids[0]],
    )
    showtime_day = _instant(str(showtime["starts_at"])).astimezone(zone).date()
    empty_day = showtime_day - timedelta(days=10)

    for params in (
        {"date_from": empty_day.isoformat(), "date_to": empty_day.isoformat()},
        {
            "group_by": "day",
            "date_from": empty_day.isoformat(),
            "date_to": (empty_day + timedelta(days=1)).isoformat(),
        },
    ):
        body = _revenue_body(await _get(client, _REVENUE_URL, headers, params))
        assert body["date_from"] == params["date_from"]
        assert body["date_to"] == params["date_to"]
        assert body["rows"] == []
        assert body["total_cents"] == 0
        assert body["tickets_sold"] == 0


async def test_revenue_reconciles_with_capacity_sold_seats_times_price(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """Revenue equals each showtime's ``sold_seats`` times its price (FR-9).

    Movie A shows twice (1000 and 2500) and movie B once (2000), with two, one
    and three confirmed seats. For each movie the revenue row and the summed
    ``sold_seats * price_cents`` of the capacity rows are the same number, and
    the totals of both reports agree (test 23).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.reconcile.admin@example.com"
    )
    user_id, _user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.reconcile@example.com"
    )
    movie_a = await create_movie_via_api(client, headers, "Spec6 Reconcile Movie A")
    movie_b = await create_movie_via_api(client, headers, "Spec6 Reconcile Movie B")
    movie_ids = {"a": int(movie_a["id"]), "b": int(movie_b["id"])}
    plans = (
        ("a_first", "a", 1_000, 2, 1),
        ("a_second", "a", 2_500, 1, 2),
        ("b_only", "b", 2_000, 3, 3),
    )
    prices: dict[int, int] = {}
    sold_by_showtime: dict[int, int] = {}
    for name, family, price_cents, seats, day_offset in plans:
        screen = await create_screen_via_api(
            client, headers, f"Spec6 Reconcile Screen {name}", 2, 2
        )
        showtime = await create_showtime_via_api(
            client,
            headers,
            movie_ids[family],
            int(screen["id"]),
            future_start(timedelta(days=day_offset)),
            price_cents,
        )
        showtime_id = int(showtime["id"])
        prices[showtime_id] = price_cents
        sold_by_showtime[showtime_id] = seats
        seat_ids = await _seat_ids(client, showtime_id)
        await _confirmed_reservation(
            rollback_connection,
            user_id=user_id,
            showtime_id=showtime_id,
            seat_ids=seat_ids[:seats],
        )

    capacity_rows, capacity_total, _limit, _offset = _page(
        await _get(
            client,
            _CAPACITY_URL,
            headers,
            {"movie_id": movie_ids["a"], "page_size": 100},
        )
    )
    revenue_rows = [
        dict(row)
        for row in _revenue_body(
            await _get(client, _REVENUE_URL, headers, {"movie_id": movie_ids["a"]})
        )["rows"]
    ]
    capacity_revenue = sum(
        int(row["sold_seats"]) * prices[int(row["showtime"]["id"])]
        for row in capacity_rows
    )
    assert [int(row["showtime"]["id"]) for row in capacity_rows] == sorted(
        int(row["showtime"]["id"]) for row in capacity_rows
    )
    assert (len(capacity_rows), capacity_total) == (2, 2)
    assert capacity_revenue == 4_500

    assert revenue_rows == [
        {
            "movie_id": movie_ids["a"],
            "title": "Spec6 Reconcile Movie A",
            "tickets_sold": 3,
            "revenue_cents": capacity_revenue,
        }
    ]

    body = _revenue_body(await _get(client, _REVENUE_URL, headers))
    assert body["total_cents"] == 10_500
    assert body["total_cents"] == sum(
        sold_by_showtime[showtime_id] * price for showtime_id, price in prices.items()
    )
    b_row = next(row for row in body["rows"] if int(row["movie_id"]) == movie_ids["b"])
    assert (b_row["tickets_sold"], b_row["revenue_cents"]) == (3, 6_000)


async def test_revenue_values_are_integers_never_floats(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """Money and counts are JSON integers, never floats or strings (test 24).

    ``principles.md`` requires integer cents, so every id, price, count and money
    field of all three reports is checked with ``type(x) is int`` -- ``bool`` and
    ``float`` fail that check -- while one confirmed seat and one hold make the
    numbers non-zero.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.types.admin@example.com"
    )
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Types Movie",
        screen_name="Spec6 Types Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_500,
        rows=2,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    owner_id, _owner_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.types.owner@example.com"
    )
    holder_id, holder_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.types.holder@example.com"
    )
    await _confirmed_reservation(
        rollback_connection,
        user_id=owner_id,
        showtime_id=showtime_id,
        seat_ids=seat_ids[:2],
    )
    held = await _hold(client, holder_headers, showtime_id, [seat_ids[2]])
    assert held.status_code == 201, held.text

    revenue = _revenue_body(await _get(client, _REVENUE_URL, headers))
    for name in ("total_cents", "tickets_sold"):
        assert type(revenue[name]) is int, (name, revenue)
    assert (revenue["total_cents"], revenue["tickets_sold"]) == (3_000, 2)
    (row,) = revenue["rows"]
    for name in ("movie_id", "tickets_sold", "revenue_cents"):
        assert type(row[name]) is int, (name, row)

    items, total, limit, offset = _page(
        await _get(client, _CAPACITY_URL, headers, {"showtime_id": showtime_id})
    )
    assert (total, limit, offset) == (1, 20, 0)
    capacity = _row_of(items, showtime_id)
    for name in ("total_seats", "sold_seats", "held_seats", "available_seats"):
        assert type(capacity[name]) is int, (name, capacity)
    for name in ("id", "price_cents"):
        assert type(capacity["showtime"][name]) is int, (name, capacity)
    assert type(capacity["showtime"]["movie"]["id"]) is int
    assert type(capacity["showtime"]["screen"]["id"]) is int
    assert (capacity["sold_seats"], capacity["held_seats"]) == (2, 1)

    admin_items, admin_total, _limit, _offset = _page(
        await _get(client, _ADMIN_RESERVATIONS_URL, headers)
    )
    assert admin_total == 2
    for item in admin_items:
        assert type(item["id"]) is int, item
        assert type(item["total_cents"]) is int, item
        assert type(item["user"]["id"]) is int, item
        assert type(item["showtime"]["id"]) is int, item
        assert type(item["showtime"]["price_cents"]) is int, item
        for seat in item["seats"]:
            assert type(seat["seat_id"]) is int, seat
            assert type(seat["seat_number"]) is int, seat
        assert item["total_cents"] == len(item["seats"]) * 1_500
    assert {int(item["user"]["id"]) for item in admin_items} == {
        owner_id,
        holder_id,
    }


async def test_cancel_removes_revenue_and_frees_capacity(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """A cancellation moves the same seats out of money and back into stock.

    Two of eight seats at 1500 are held and confirmed: revenue is 3000 and
    capacity reads sold 2 of 8. After the user cancels through the API, revenue
    is zero with no rows and capacity reads sold 0, held 0, available 8 -- the
    two reports never disagree about the same reservation (FR-9, test 25).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.cancel.admin@example.com"
    )
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Cancel Movie",
        screen_name="Spec6 Cancel Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_500,
        rows=2,
        seats_per_row=4,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.cancel@example.com"
    )
    held = await _hold(client, user_headers, showtime_id, seat_ids[:2])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    assert (await _confirm(client, user_headers, reservation_id)).status_code == 200

    before = _row_of(
        _page(await _get(client, _CAPACITY_URL, headers, {"showtime_id": showtime_id}))[
            0
        ],
        showtime_id,
    )
    assert (before["sold_seats"], before["available_seats"]) == (2, 6)
    revenue_before = _revenue_body(await _get(client, _REVENUE_URL, headers))
    assert (revenue_before["total_cents"], revenue_before["tickets_sold"]) == (
        3_000,
        2,
    )

    cancelled = await client.post(
        f"/reservations/{reservation_id}/cancel", headers=user_headers
    )
    assert cancelled.status_code == 200, cancelled.text

    after = _row_of(
        _page(await _get(client, _CAPACITY_URL, headers, {"showtime_id": showtime_id}))[
            0
        ],
        showtime_id,
    )
    assert (after["sold_seats"], after["held_seats"]) == (0, 0)
    assert (after["total_seats"], after["available_seats"]) == (8, 8)
    assert after["showtime"]["id"] == before["showtime"]["id"]

    revenue_after = _revenue_body(await _get(client, _REVENUE_URL, headers))
    assert revenue_after["rows"] == []
    assert (revenue_after["total_cents"], revenue_after["tickets_sold"]) == (0, 0)

    items, total, _limit, _offset = _page(
        await _get(client, _ADMIN_RESERVATIONS_URL, headers)
    )
    assert total == 1
    assert items[0]["status"] == _CANCELLED
    assert items[0]["cancelled_at"] is not None


async def test_reports_are_read_only(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """Reading a report never persists an expiry (FR-8, test 26).

    A hold older than ``HOLD_MINUTES`` is reported as ``expired`` by the admin
    list and is not counted as held by capacity, yet nothing is written: the row
    count, the stored status of every reservation and the stale row's lifecycle
    instants are identical before and after all three reports are read with a
    range, a status filter and a day grouping.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.readonly.admin@example.com"
    )
    zone = _cinema_zone()
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Readonly Movie",
        screen_name="Spec6 Readonly Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_000,
        rows=2,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    owner_id, _owner_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.readonly.owner@example.com"
    )
    _holder_id, holder_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.readonly.holder@example.com"
    )
    confirmed_id = await _confirmed_reservation(
        rollback_connection,
        user_id=owner_id,
        showtime_id=showtime_id,
        seat_ids=seat_ids[:1],
    )
    stale = await _hold(client, holder_headers, showtime_id, seat_ids[1:3])
    assert stale.status_code == 201, stale.text
    stale_id = int(_json_body(stale)["id"])
    await _set_reservation_created_at(
        rollback_connection,
        stale_id,
        datetime.now(UTC) - _hold_window() - timedelta(minutes=1),
    )

    counts_before = await _reservation_row_counts(rollback_connection)
    confirmed_status_before = await _stored_reservation_status(
        rollback_connection, confirmed_id
    )
    stale_fields_before = await _stored_reservation_fields(
        rollback_connection, stale_id
    )
    assert stale_fields_before[0] == _HELD
    showtime_day = _instant(str(showtime["starts_at"])).astimezone(zone).date()

    expired_items, expired_total, _limit, _offset = _page(
        await _get(
            client,
            _ADMIN_RESERVATIONS_URL,
            headers,
            {"status": "expired", "showtime_id": showtime_id},
        )
    )
    assert expired_total == 1
    assert [int(item["id"]) for item in expired_items] == [stale_id]
    assert expired_items[0]["status"] == _EXPIRED

    capacity = _row_of(
        _page(await _get(client, _CAPACITY_URL, headers, {"showtime_id": showtime_id}))[
            0
        ],
        showtime_id,
    )
    assert (capacity["sold_seats"], capacity["held_seats"]) == (1, 0)
    assert capacity["available_seats"] == 5

    for params in (
        {},
        {"date_from": showtime_day.isoformat(), "date_to": showtime_day.isoformat()},
        {
            "group_by": "day",
            "date_from": showtime_day.isoformat(),
            "date_to": showtime_day.isoformat(),
        },
    ):
        _revenue_body(await _get(client, _REVENUE_URL, headers, params))
    _assert_error(
        await _get(client, _REVENUE_URL, headers, {"group_by": "week"}),
        422,
        "VALIDATION_ERROR",
    )

    await _assert_rows_unchanged(rollback_connection, counts_before)
    assert (
        await _stored_reservation_status(rollback_connection, confirmed_id)
        == confirmed_status_before
    )
    assert (
        await _stored_reservation_fields(rollback_connection, stale_id)
        == stale_fields_before
    )


async def test_spec5_list_unchanged_after_shared_filter_refactor(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """``GET /reservations`` still filters on the effective status (test 27).

    The reporting module reuses the SPEC-5 status filter, so a short check of
    the owner's own list guards that refactor: four reservations in the four
    states, one of them a stale ``held`` row, filter one-to-one, the stale row
    only ever appears under ``expired``, and the unfiltered list still reports
    it as ``expired`` while storage keeps ``held``.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.spec5.admin@example.com"
    )
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Spec5 Movie",
        screen_name="Spec6 Spec5 Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_000,
        rows=2,
        seats_per_row=4,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    user_id, user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.spec5@example.com"
    )

    # A user holds one reservation per showtime at a time, so each hold is
    # settled -- confirmed, cancelled or left as the only live hold -- before
    # the next one is taken.
    confirmed = await _hold(client, user_headers, showtime_id, [seat_ids[1]])
    assert confirmed.status_code == 201, confirmed.text
    confirmed_id = int(_json_body(confirmed)["id"])
    assert (await _confirm(client, user_headers, confirmed_id)).status_code == 200

    stored_cancelled = await _hold(client, user_headers, showtime_id, [seat_ids[2]])
    assert stored_cancelled.status_code == 201, stored_cancelled.text
    cancelled_id = int(_json_body(stored_cancelled)["id"])
    await _set_reservation_status(rollback_connection, cancelled_id, _CANCELLED)

    fresh = await _hold(client, user_headers, showtime_id, [seat_ids[0]])
    assert fresh.status_code == 201, fresh.text
    fresh_id = int(_json_body(fresh)["id"])

    stale_id = await _insert_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=showtime_id,
        seat_ids=[seat_ids[3]],
        status=_HELD,
        created_at=datetime.now(UTC) - _hold_window() - timedelta(minutes=1),
    )

    expected = {
        _HELD: [fresh_id],
        _CONFIRMED: [confirmed_id],
        _CANCELLED: [cancelled_id],
        _EXPIRED: [stale_id],
    }
    for status, wanted_ids in expected.items():
        ids, total = await _ids_and_total(
            client, "/reservations", user_headers, {"status": status}
        )
        assert (ids, total) == (wanted_ids, 1), status

    ids, total = await _ids_and_total(client, "/reservations", user_headers)
    assert total == 4
    assert set(ids) == {fresh_id, confirmed_id, cancelled_id, stale_id}
    items, _total, _limit, _offset = _page(
        await _get(client, "/reservations", user_headers)
    )
    statuses = {int(item["id"]): item["status"] for item in items}
    assert statuses == {
        fresh_id: _HELD,
        confirmed_id: _CONFIRMED,
        cancelled_id: _CANCELLED,
        stale_id: _EXPIRED,
    }
    assert await _stored_reservation_status(rollback_connection, stale_id) == _HELD


async def test_error_bodies_use_standard_shape_for_new_code(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """Every failing report request keeps the standard error body (FR-12).

    The new ``INVALID_DATE_RANGE`` code is rendered exactly like the existing
    ones: ``{"error": {"code", "message"}}`` and nothing else, for 401, 403,
    422 shape errors, 422 range errors and a 404 from a neighbouring route, with
    no reservation or seat row written by any of them (test 28).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec6.errors.admin@example.com"
    )
    _movie, _screen, showtime = await _add_showtime(
        client,
        headers,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        title="Spec6 Errors Movie",
        screen_name="Spec6 Errors Screen",
        starts_at=future_start(timedelta(days=1)),
        price_cents=1_000,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, user_headers = await _create_account(
        client, create_user, login, auth_header, "spec6.errors@example.com"
    )
    held = await _hold(client, user_headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    before = await _reservation_row_counts(rollback_connection)
    local_today = datetime.now(UTC).astimezone(_cinema_zone()).date()
    today = local_today.isoformat()
    yesterday = (local_today - timedelta(days=1)).isoformat()
    reversed_range = {"date_from": today, "date_to": yesterday}
    day_without_range = {"group_by": "day"}

    # ``headers`` is ``None`` for the anonymous case and ``user_headers`` for the
    # plain-user cases; every other request acts as the administrator.
    failures: tuple[
        tuple[str, str, dict[str, str] | None, dict[str, Any] | None, int, str], ...
    ] = (
        (
            "admin list, anonymous",
            _ADMIN_RESERVATIONS_URL,
            None,
            None,
            401,
            "UNAUTHENTICATED",
        ),
        (
            "capacity, plain user",
            _CAPACITY_URL,
            user_headers,
            None,
            403,
            "FORBIDDEN",
        ),
        (
            "revenue, plain user",
            _REVENUE_URL,
            user_headers,
            None,
            403,
            "FORBIDDEN",
        ),
        (
            "admin list, limit 0",
            _ADMIN_RESERVATIONS_URL,
            headers,
            {"limit": 0},
            422,
            "VALIDATION_ERROR",
        ),
        (
            "capacity, non-integer id",
            _CAPACITY_URL,
            headers,
            {"showtime_id": "abc"},
            422,
            "VALIDATION_ERROR",
        ),
        (
            "revenue, unknown group_by",
            _REVENUE_URL,
            headers,
            {"group_by": "week"},
            422,
            "VALIDATION_ERROR",
        ),
        (
            "admin list, reversed range",
            _ADMIN_RESERVATIONS_URL,
            headers,
            reversed_range,
            422,
            "INVALID_DATE_RANGE",
        ),
        (
            "capacity, reversed range",
            _CAPACITY_URL,
            headers,
            reversed_range,
            422,
            "INVALID_DATE_RANGE",
        ),
        (
            "revenue, day without range",
            _REVENUE_URL,
            headers,
            day_without_range,
            422,
            "INVALID_DATE_RANGE",
        ),
        (
            "revenue, day with one date",
            _REVENUE_URL,
            headers,
            {"group_by": "day", "date_from": today},
            422,
            "INVALID_DATE_RANGE",
        ),
    )

    for label, url, request_headers, params, status, code in failures:
        response = await _get(client, url, request_headers, params)
        assert response.status_code == status, (label, response.text)
        assert _error_payload(response)["code"] == code, (label, response.text)

    # The standard shape is not specific to the reporting routes: a 404 from the
    # seat map renders through the same handler.
    not_found = await _get(client, "/showtimes/987654321/seats")
    assert not_found.status_code == 404, not_found.text
    assert _error_payload(not_found)["code"] == "SHOWTIME_NOT_FOUND", not_found.text

    await _assert_rows_unchanged(rollback_connection, before)
