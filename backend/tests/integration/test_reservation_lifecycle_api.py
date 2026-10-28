"""Integration tests for the SPEC-5 reservation lifecycle API (tests 3-19).

The two endpoints under test are ``GET /reservations`` and
``POST /reservations/{reservation_id}/cancel``. Every test creates its own
administrator, movie, screen, showtime, user and reservations inside the
rolled-back connection that :mod:`tests.conftest` shares with the application,
so no test depends on seeded data and no test commits.

The setup builders (hold, confirm, release, direct SQL inserts) live in
``tests/conftest.py`` because this module needs them too; importing them keeps
one definition of each builder. Only the list and cancel requests plus the
assertions specific to SPEC-5 are declared here.

The clock is always the real one: every instant is ``now`` plus an offset, and
the three time-dependent tests (6, 7 and 14) either backdate a row by SQL or
call the service with an explicit ``now``, so boundaries are exact instead of
racy.

Groups: FR-1..FR-5 are the list endpoint (tests 3-9) and FR-6..FR-12 the
cancel endpoint (tests 10-19). The concurrency tests 20-21 are out of scope.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from src.core.config import get_settings
from src.core.errors import ApiError
from src.reservations import service
from tests.conftest import (
    _CANCELLED,
    _CONFIRMED,
    _EXPIRED,
    _HELD,
    _RESERVATIONS_URL,
    _UNKNOWN_ID,
    _AdminHeaders,
    _assert_error,
    _assert_rows_unchanged,
    _assert_status_consistent,
    _assert_stored_status,
    _AuthHeader,
    _confirm,
    _count_user_reservations,
    _create_account,
    _create_screening,
    _CreateMovie,
    _CreateScreen,
    _CreateShowtime,
    _CreateUser,
    _error_payload,
    _FutureStart,
    _hold,
    _insert_reservation,
    _insert_showtime,
    _json_body,
    _Login,
    _parse_optional_instant,
    _reservation_row_counts,
    _screen_seat_ids,
    _seat_ids,
    _seat_map,
    _set_reservation_created_at,
    _set_reservation_status,
    _states_by_seat,
    _stored_reservation_fields,
    _stored_reservation_status,
    _stored_seat_statuses,
)

_CANCEL_TEMPLATE = "/reservations/{reservation_id}/cancel"


async def _cancel(
    client: httpx.AsyncClient,
    headers: dict[str, str] | None,
    reservation_id: int,
) -> httpx.Response:
    """Post a cancel request and return its raw response.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting user's ``Authorization`` header, or ``None`` to
            send an unauthenticated request.
        reservation_id: The reservation to cancel.

    Returns:
        httpx.Response: The unasserted response, so the caller reports a
        failure with the body attached.
    """
    return await client.post(
        _CANCEL_TEMPLATE.format(reservation_id=reservation_id),
        headers=headers,
    )


async def _list(
    client: httpx.AsyncClient,
    headers: dict[str, str] | None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Get one page of reservations and return the raw response.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting user's ``Authorization`` header, or ``None`` to
            send an unauthenticated request.
        params: Query parameters such as ``status``, ``upcoming``, ``limit`` or
            ``offset``; ``None`` sends no query string at all.

    Returns:
        httpx.Response: The unasserted response.
    """
    return await client.get(_RESERVATIONS_URL, headers=headers, params=params)


def _page(response: httpx.Response) -> tuple[list[dict[str, Any]], int]:
    """Assert a 200 page response and return its items and total.

    Args:
        response: The raw response of a ``GET /reservations`` request.

    Returns:
        tuple: The page's items in the order the API returned them, and the
        unpaginated ``total``.

    Raises:
        AssertionError: If the status is not 200 or the body is not the SPEC-3
            envelope ``{"items", "total", "limit", "offset"}``.
    """
    assert response.status_code == 200, response.text
    body = _json_body(response)
    assert set(body) == {"items", "total", "limit", "offset"}, body
    items = body["items"]
    assert isinstance(items, list), body
    for item in items:
        assert isinstance(item, dict), item
    return [dict(item) for item in items], int(body["total"])


def _ids(items: list[dict[str, Any]]) -> list[int]:
    """Return the reservation ids of a page in the order the API returned them.

    Args:
        items: The page's items.

    Returns:
        list[int]: One id per item, in page order.
    """
    return [int(item["id"]) for item in items]


def _item(items: list[dict[str, Any]], reservation_id: int) -> dict[str, Any]:
    """Return the list item of one reservation.

    Args:
        items: The page's items.
        reservation_id: The id to look for.

    Returns:
        dict[str, Any]: The matching item.

    Raises:
        AssertionError: If the page does not contain that reservation, with the
            ids it does contain attached.
    """
    for item in items:
        if int(item["id"]) == reservation_id:
            return item
    raise AssertionError(f"reservation {reservation_id} not in {_ids(items)}")


async def test_list_requires_authentication_returns_401(
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
    """Listing without credentials is a 401 that writes nothing (FR-1, test 3).

    A real reservation exists first, so the "nothing was written" check runs
    against a non-empty table.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.list.auth.admin@example.com",
        title="Spec5 List Auth Movie",
        screen_name="Spec5 List Auth Screen",
        rows=1,
        seats_per_row=1,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.list.auth.user@example.com",
    )
    held = await _hold(client, headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    before = await _reservation_row_counts(rollback_connection)

    response = await _list(client, None)

    _assert_error(response, 401, "UNAUTHENTICATED")
    await _assert_rows_unchanged(rollback_connection, before)
    assert _json_body(held)["status"] == _HELD


async def test_list_returns_only_own_reservations_newest_first(
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
    """The list is owner-scoped and newest first (FR-1, FR-2, test 4).

    The acting user owns three reservations on one showtime (two confirmed and
    one held); a second user owns one reservation on another showtime. Neither
    list may contain the other user's rows, and the order is
    ``created_at DESC, id DESC``.
    """
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.own.admin@example.com",
        title="Spec5 Own Movie",
        screen_name="Spec5 Own Screen",
        rows=1,
        seats_per_row=3,
    )
    _other_admin, _other_movie, _other_screen, other_showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.own.other.admin@example.com",
        title="Spec5 Own Other Movie",
        screen_name="Spec5 Own Other Screen",
        rows=1,
        seats_per_row=1,
    )
    showtime_id = int(showtime["id"])
    other_showtime_id = int(other_showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    other_seat_ids = await _seat_ids(client, other_showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.own.user@example.com",
    )
    _other_id, other_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.own.other.user@example.com",
    )

    first_hold = await _hold(client, headers, showtime_id, [seat_ids[0]])
    assert first_hold.status_code == 201, first_hold.text
    first_id = int(_json_body(first_hold)["id"])
    first_confirm = await _confirm(client, headers, first_id)
    assert first_confirm.status_code == 200, first_confirm.text

    second_hold = await _hold(client, headers, showtime_id, [seat_ids[1]])
    assert second_hold.status_code == 201, second_hold.text
    second_id = int(_json_body(second_hold)["id"])
    second_confirm = await _confirm(client, headers, second_id)
    assert second_confirm.status_code == 200, second_confirm.text

    third_hold = await _hold(client, headers, showtime_id, [seat_ids[2]])
    assert third_hold.status_code == 201, third_hold.text
    third_id = int(_json_body(third_hold)["id"])

    other_hold = await _hold(client, other_headers, other_showtime_id, other_seat_ids)
    assert other_hold.status_code == 201, other_hold.text
    other_id = int(_json_body(other_hold)["id"])

    own_items, own_total = _page(await _list(client, headers))

    assert own_total == 3
    assert _ids(own_items) == [third_id, second_id, first_id]
    assert {int(item["showtime_id"]) for item in own_items} == {showtime_id}
    assert other_id not in _ids(own_items)

    other_items, other_total = _page(await _list(client, other_headers))

    assert other_total == 1
    assert _ids(other_items) == [other_id]
    assert {int(item["showtime_id"]) for item in other_items} == {other_showtime_id}
    assert not {first_id, second_id, third_id} & set(_ids(other_items))


async def test_list_item_embeds_showtime_movie_screen_and_seats(
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
    """Each item embeds its showtime, movie, screen and seats (FR-2, test 5).

    The held reservation's ``expires_at`` is derived from ``created_at`` and the
    configured hold window, and ``total_cents`` is the price times the seat
    count, never a stored value.
    """
    settings = get_settings()
    _admin, movie, screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.embed.admin@example.com",
        title="Spec5 Embed Movie",
        screen_name="Spec5 Embed Screen",
        rows=1,
        seats_per_row=3,
        price_cents=1_250,
        start_offset=timedelta(days=2),
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.embed.user@example.com",
    )
    held = await _hold(client, headers, showtime_id, [seat_ids[2], seat_ids[0]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])

    items, total = _page(await _list(client, headers))

    assert total == 1
    item = _item(items, reservation_id)
    assert item["status"] == _HELD
    assert int(item["showtime_id"]) == showtime_id
    assert item["total_cents"] == 2_500
    seats_body = item["seats"]
    assert isinstance(seats_body, list), item
    assert [int(seat["seat_id"]) for seat in seats_body] == [seat_ids[0], seat_ids[2]]
    created_at = _parse_optional_instant(item["created_at"])
    expires_at = _parse_optional_instant(item["expires_at"])
    assert created_at is not None
    assert expires_at == created_at + timedelta(minutes=settings.hold_minutes)
    assert _parse_optional_instant(item["confirmed_at"]) is None
    assert _parse_optional_instant(item["cancelled_at"]) is None

    showtime_body = item["showtime"]
    assert isinstance(showtime_body, dict), item
    assert int(showtime_body["id"]) == showtime_id
    assert _parse_optional_instant(showtime_body["starts_at"]) == (
        _parse_optional_instant(showtime["starts_at"])
    )
    assert showtime_body["price_cents"] == 1_250
    assert showtime_body["movie"] == {
        "id": int(movie["id"]),
        "title": "Spec5 Embed Movie",
        "poster_url": None,
    }
    assert showtime_body["screen"] == {
        "id": int(screen["id"]),
        "name": "Spec5 Embed Screen",
    }


async def test_list_status_filter_uses_effective_status(
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
    """The status filter matches the effective status (FR-3, test 6).

    Four reservations are prepared: a fresh ``held``, a stored-``held`` row
    backdated past the hold window by SQL (effective ``expired``), a
    ``confirmed`` and a ``cancelled`` one. The stale hold sits on a second
    showtime so no later hold lazily expires it, which keeps the row stored
    ``held`` while every read reports ``expired`` -- and the reads write
    nothing.
    """
    settings = get_settings()
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.filter.admin@example.com",
        title="Spec5 Filter Movie",
        screen_name="Spec5 Filter Screen",
        rows=1,
        seats_per_row=3,
    )
    _stale_admin, _stale_movie, _stale_screen, stale_showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.filter.stale.admin@example.com",
        title="Spec5 Filter Stale Movie",
        screen_name="Spec5 Filter Stale Screen",
        rows=1,
        seats_per_row=1,
    )
    showtime_id = int(showtime["id"])
    stale_showtime_id = int(stale_showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    stale_seat_ids = await _seat_ids(client, stale_showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.filter.user@example.com",
    )

    confirmed_hold = await _hold(client, headers, showtime_id, [seat_ids[0]])
    assert confirmed_hold.status_code == 201, confirmed_hold.text
    confirmed_id = int(_json_body(confirmed_hold)["id"])
    confirmed_response = await _confirm(client, headers, confirmed_id)
    assert confirmed_response.status_code == 200, confirmed_response.text

    cancelled_hold = await _hold(client, headers, showtime_id, [seat_ids[1]])
    assert cancelled_hold.status_code == 201, cancelled_hold.text
    cancelled_id = int(_json_body(cancelled_hold)["id"])
    cancelled_confirm = await _confirm(client, headers, cancelled_id)
    assert cancelled_confirm.status_code == 200, cancelled_confirm.text
    cancelled_cancel = await _cancel(client, headers, cancelled_id)
    assert cancelled_cancel.status_code == 200, cancelled_cancel.text

    fresh_hold = await _hold(client, headers, showtime_id, [seat_ids[2]])
    assert fresh_hold.status_code == 201, fresh_hold.text
    fresh_id = int(_json_body(fresh_hold)["id"])

    stale_hold = await _hold(client, headers, stale_showtime_id, [stale_seat_ids[0]])
    assert stale_hold.status_code == 201, stale_hold.text
    stale_id = int(_json_body(stale_hold)["id"])
    stale_created_at = datetime.now(UTC) - timedelta(minutes=settings.hold_minutes + 1)
    await _set_reservation_created_at(rollback_connection, stale_id, stale_created_at)

    items, total = _page(await _list(client, headers))

    assert total == 4
    assert set(_ids(items)) == {confirmed_id, cancelled_id, fresh_id, stale_id}
    statuses = {int(item["id"]): item["status"] for item in items}
    assert statuses[confirmed_id] == _CONFIRMED
    assert statuses[cancelled_id] == _CANCELLED
    assert statuses[fresh_id] == _HELD
    assert statuses[stale_id] == _EXPIRED
    assert _item(items, stale_id)["expires_at"] is None
    assert _item(items, fresh_id)["expires_at"] is not None

    held_items, held_total = _page(await _list(client, headers, {"status": "held"}))
    assert _ids(held_items) == [fresh_id]
    assert held_total == 1

    expired_items, expired_total = _page(
        await _list(client, headers, {"status": "expired"})
    )
    assert _ids(expired_items) == [stale_id]
    assert expired_total == 1

    confirmed_items, confirmed_total = _page(
        await _list(client, headers, {"status": "confirmed"})
    )
    assert _ids(confirmed_items) == [confirmed_id]
    assert confirmed_total == 1

    cancelled_items, cancelled_total = _page(
        await _list(client, headers, {"status": "cancelled"})
    )
    assert _ids(cancelled_items) == [cancelled_id]
    assert cancelled_total == 1

    # Reads never write: the stale hold is still stored as held.
    assert await _stored_reservation_status(rollback_connection, stale_id) == _HELD


async def test_list_upcoming_filter_true_false_and_boundary(
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
    """``upcoming`` splits on the showtime start, boundary included (FR-4, test 7).

    The service is called with an explicit ``now`` so the boundary is exact: a
    showtime starting at ``now`` counts as started (not upcoming) and one
    starting at ``now + 1 s`` counts as upcoming.
    """
    _admin, movie, screen, _showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.upcoming.admin@example.com",
        title="Spec5 Upcoming Movie",
        screen_name="Spec5 Upcoming Screen",
        rows=1,
        seats_per_row=2,
    )
    movie_id = int(movie["id"])
    screen_id = int(screen["id"])
    seat_ids = await _screen_seat_ids(rollback_connection, screen_id)
    now = datetime.now(UTC)
    started_showtime_id = await _insert_showtime(
        rollback_connection,
        movie_id=movie_id,
        screen_id=screen_id,
        starts_at=now,
        price_cents=700,
    )
    upcoming_showtime_id = await _insert_showtime(
        rollback_connection,
        movie_id=movie_id,
        screen_id=screen_id,
        starts_at=now + timedelta(seconds=1),
        price_cents=900,
    )
    user_id, _headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.upcoming.user@example.com",
    )
    started_id = await _insert_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=started_showtime_id,
        seat_ids=[seat_ids[0]],
        status=_HELD,
        created_at=now - timedelta(minutes=1),
    )
    upcoming_id = await _insert_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=upcoming_showtime_id,
        seat_ids=[seat_ids[1]],
        status=_HELD,
        created_at=now,
    )

    upcoming_items, upcoming_total = await service.list_reservations(
        rollback_connection,
        user_id,
        None,
        True,
        20,
        0,
        now,
    )
    assert [item.id for item in upcoming_items] == [upcoming_id]
    assert upcoming_total == 1
    assert upcoming_items[0].showtime.starts_at == now + timedelta(seconds=1)

    past_items, past_total = await service.list_reservations(
        rollback_connection,
        user_id,
        None,
        False,
        20,
        0,
        now,
    )
    assert [item.id for item in past_items] == [started_id]
    assert past_total == 1
    assert past_items[0].showtime.starts_at == now

    all_items, all_total = await service.list_reservations(
        rollback_connection,
        user_id,
        None,
        None,
        20,
        0,
        now,
    )
    assert [item.id for item in all_items] == [upcoming_id, started_id]
    assert all_total == 2
    assert all_items[0].status == _HELD
    assert all_items[1].status == _HELD


async def test_list_pagination_total_and_bounds_return_422(
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
    """Pagination returns whole pages with the unpaginated total (FR-5, test 8).

    ``total`` ignores the window, an offset past the end is an empty page rather
    than an error, and an out-of-range ``limit`` or ``offset`` is a 422 that
    writes nothing.
    """
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.page.admin@example.com",
        title="Spec5 Page Movie",
        screen_name="Spec5 Page Screen",
        rows=1,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.page.user@example.com",
    )

    first_hold = await _hold(client, headers, showtime_id, [seat_ids[0]])
    assert first_hold.status_code == 201, first_hold.text
    first_id = int(_json_body(first_hold)["id"])
    assert (await _confirm(client, headers, first_id)).status_code == 200

    second_hold = await _hold(client, headers, showtime_id, [seat_ids[1]])
    assert second_hold.status_code == 201, second_hold.text
    second_id = int(_json_body(second_hold)["id"])
    assert (await _confirm(client, headers, second_id)).status_code == 200

    third_hold = await _hold(client, headers, showtime_id, [seat_ids[2]])
    assert third_hold.status_code == 201, third_hold.text
    third_id = int(_json_body(third_hold)["id"])

    first_response = await _list(client, headers, {"limit": 2, "offset": 0})
    first_items, first_total = _page(first_response)
    first_envelope = _json_body(first_response)
    assert first_total == 3
    assert _ids(first_items) == [third_id, second_id]
    assert first_envelope["limit"] == 2
    assert first_envelope["offset"] == 0

    second_items, second_total = _page(
        await _list(client, headers, {"limit": 2, "offset": 2})
    )
    assert second_total == 3
    assert _ids(second_items) == [first_id]

    empty_items, empty_total = _page(
        await _list(client, headers, {"limit": 1, "offset": 5})
    )
    assert empty_items == []
    assert empty_total == 3

    before = await _reservation_row_counts(rollback_connection)
    for invalid in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        _assert_error(
            await _list(client, headers, invalid),
            422,
            "VALIDATION_ERROR",
        )
    await _assert_rows_unchanged(rollback_connection, before)


async def test_list_is_empty_for_user_without_reservations(
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
    """A user with no reservations gets an empty 200 page (FR-1, test 9).

    Another user's reservation exists, so the empty page proves the ownership
    filter rather than an empty database.
    """
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.empty.admin@example.com",
        title="Spec5 Empty Movie",
        screen_name="Spec5 Empty Screen",
        rows=1,
        seats_per_row=1,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _owner_id, owner_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.empty.owner@example.com",
    )
    _other_id, other_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.empty.other@example.com",
    )
    held = await _hold(client, owner_headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text

    response = await _list(client, other_headers)

    assert response.status_code == 200, response.text
    body = _json_body(response)
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 20
    assert body["offset"] == 0


async def test_cancel_confirmed_upcoming_marks_reservation_and_seat_rows_cancelled(
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
    """Cancelling a confirmed reservation cancels its rows too (FR-6, test 10).

    The reservation and every one of its seat rows must store ``cancelled``,
    ``cancelled_at`` must be set while ``confirmed_at`` is kept, and the
    response is the SPEC-4 ``ReservationOut`` shape.
    """
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.cancel.admin@example.com",
        title="Spec5 Cancel Movie",
        screen_name="Spec5 Cancel Screen",
        rows=1,
        seats_per_row=3,
        price_cents=1_500,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.cancel.user@example.com",
    )
    held = await _hold(client, headers, showtime_id, [seat_ids[0], seat_ids[2]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    confirmed = await _confirm(client, headers, reservation_id)
    assert confirmed.status_code == 200, confirmed.text

    response = await _cancel(client, headers, reservation_id)

    assert response.status_code == 200, response.text
    body = _json_body(response)
    assert int(body["id"]) == reservation_id
    assert int(body["showtime_id"]) == showtime_id
    assert body["status"] == _CANCELLED
    assert body["total_cents"] == 3_000
    assert body["expires_at"] is None
    assert _parse_optional_instant(body["confirmed_at"]) is not None
    created_at = _parse_optional_instant(body["created_at"])
    cancelled_at = _parse_optional_instant(body["cancelled_at"])
    assert created_at is not None
    assert cancelled_at is not None
    assert cancelled_at >= created_at
    seats_body = body["seats"]
    assert isinstance(seats_body, list), body
    assert [int(seat["seat_id"]) for seat in seats_body] == [seat_ids[0], seat_ids[2]]

    (
        status,
        stored_created_at,
        stored_confirmed_at,
        stored_cancelled_at,
    ) = await _stored_reservation_fields(rollback_connection, reservation_id)
    assert status == _CANCELLED
    assert stored_created_at == created_at
    assert stored_confirmed_at is not None
    assert stored_cancelled_at is not None
    await _assert_stored_status(rollback_connection, reservation_id, _CANCELLED)
    assert await _stored_seat_statuses(rollback_connection, reservation_id) == [
        _CANCELLED,
        _CANCELLED,
    ]
    await _assert_status_consistent(rollback_connection)


async def test_cancelled_seats_can_be_held_again_immediately(
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
    """Cancelling frees the seats at once for another user (FR-6, test 11).

    After the cancel, another user's hold of the same seats is a 201 and the
    cancelled reservation stays behind as history.
    """
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.frees.admin@example.com",
        title="Spec5 Frees Movie",
        screen_name="Spec5 Frees Screen",
        rows=1,
        seats_per_row=2,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _owner_id, owner_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.frees.owner@example.com",
    )
    _other_id, other_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.frees.other@example.com",
    )
    held = await _hold(client, owner_headers, showtime_id, seat_ids)
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    confirmed = await _confirm(client, owner_headers, reservation_id)
    assert confirmed.status_code == 200, confirmed.text

    cancelled = await _cancel(client, owner_headers, reservation_id)
    assert cancelled.status_code == 200, cancelled.text

    reheld = await _hold(client, other_headers, showtime_id, seat_ids)

    assert reheld.status_code == 201, reheld.text
    reheld_body = _json_body(reheld)
    assert reheld_body["status"] == _HELD
    assert int(reheld_body["id"]) != reservation_id
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "held"
    assert states[seat_ids[1]] == "held"
    await _assert_stored_status(rollback_connection, reservation_id, _CANCELLED)
    await _assert_status_consistent(rollback_connection)


async def test_cancel_held_reservation_returns_409_not_confirmed(
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
    """A held reservation is released, not cancelled (FR-7, test 12).

    The refusal is a 409 ``RESERVATION_NOT_CONFIRMED`` and writes nothing: the
    reservation and its seat row stay ``held`` with no ``cancelled_at``.
    """
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.notconfirmed.admin@example.com",
        title="Spec5 Not Confirmed Movie",
        screen_name="Spec5 Not Confirmed Screen",
        rows=1,
        seats_per_row=1,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.notconfirmed.user@example.com",
    )
    held = await _hold(client, headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    fields_before = await _stored_reservation_fields(
        rollback_connection,
        reservation_id,
    )
    before = await _reservation_row_counts(rollback_connection)

    response = await _cancel(client, headers, reservation_id)

    _assert_error(response, 409, "RESERVATION_NOT_CONFIRMED")
    await _assert_rows_unchanged(rollback_connection, before)
    assert (
        await _stored_reservation_fields(rollback_connection, reservation_id)
    ) == fields_before
    await _assert_stored_status(rollback_connection, reservation_id, _HELD)
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "held"


async def test_cancel_cancelled_or_expired_returns_409_not_confirmed(
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
    """Only a confirmed reservation can be cancelled (FR-7, test 13).

    Three non-confirmed states are refused with 409
    ``RESERVATION_NOT_CONFIRMED`` and no write: an already ``cancelled`` row, a
    stored ``expired`` row, and a ``held`` row backdated past the hold window
    (effective ``expired``). Cancel is deliberately not idempotent, so the
    second cancel of the first case is refused too.
    """
    settings = get_settings()
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.states.admin@example.com",
        title="Spec5 States Movie",
        screen_name="Spec5 States Screen",
        rows=1,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _first_id, first_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.states.first@example.com",
    )
    _second_id, second_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.states.second@example.com",
    )
    _third_id, third_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.states.third@example.com",
    )

    cancelled_hold = await _hold(client, first_headers, showtime_id, [seat_ids[0]])
    assert cancelled_hold.status_code == 201, cancelled_hold.text
    cancelled_id = int(_json_body(cancelled_hold)["id"])
    assert (await _confirm(client, first_headers, cancelled_id)).status_code == 200
    assert (await _cancel(client, first_headers, cancelled_id)).status_code == 200
    cancelled_fields = await _stored_reservation_fields(
        rollback_connection,
        cancelled_id,
    )
    before = await _reservation_row_counts(rollback_connection)

    second_cancel = await _cancel(client, first_headers, cancelled_id)

    _assert_error(second_cancel, 409, "RESERVATION_NOT_CONFIRMED")
    await _assert_rows_unchanged(rollback_connection, before)
    assert (
        await _stored_reservation_fields(rollback_connection, cancelled_id)
    ) == cancelled_fields

    expired_hold = await _hold(client, second_headers, showtime_id, [seat_ids[1]])
    assert expired_hold.status_code == 201, expired_hold.text
    expired_id = int(_json_body(expired_hold)["id"])
    await _set_reservation_status(rollback_connection, expired_id, _EXPIRED)
    expired_fields = await _stored_reservation_fields(rollback_connection, expired_id)
    before = await _reservation_row_counts(rollback_connection)

    expired_cancel = await _cancel(client, second_headers, expired_id)

    _assert_error(expired_cancel, 409, "RESERVATION_NOT_CONFIRMED")
    await _assert_rows_unchanged(rollback_connection, before)
    assert (
        await _stored_reservation_fields(rollback_connection, expired_id)
    ) == expired_fields
    assert await _stored_reservation_status(rollback_connection, expired_id) == _EXPIRED

    stale_hold = await _hold(client, third_headers, showtime_id, [seat_ids[2]])
    assert stale_hold.status_code == 201, stale_hold.text
    stale_id = int(_json_body(stale_hold)["id"])
    await _set_reservation_created_at(
        rollback_connection,
        stale_id,
        datetime.now(UTC) - timedelta(minutes=settings.hold_minutes + 1),
    )
    stale_fields = await _stored_reservation_fields(rollback_connection, stale_id)
    before = await _reservation_row_counts(rollback_connection)

    stale_cancel = await _cancel(client, third_headers, stale_id)

    _assert_error(stale_cancel, 409, "RESERVATION_NOT_CONFIRMED")
    await _assert_rows_unchanged(rollback_connection, before)
    assert (
        await _stored_reservation_fields(rollback_connection, stale_id)
    ) == stale_fields
    await _assert_stored_status(rollback_connection, stale_id, _HELD)


async def test_cancel_started_showtime_returns_409_boundary(
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
    """The cancel window closes exactly when the showtime starts (FR-9, test 14).

    The service is called with an explicit ``now``: ``starts_at == now`` is
    refused with 409 ``SHOWTIME_STARTED`` and writes nothing, while
    ``starts_at == now + 1 s`` is cancelled with ``cancelled_at == now``.
    """
    _admin, movie, screen, _showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.started.admin@example.com",
        title="Spec5 Started Movie",
        screen_name="Spec5 Started Screen",
        rows=1,
        seats_per_row=2,
    )
    movie_id = int(movie["id"])
    screen_id = int(screen["id"])
    seat_ids = await _screen_seat_ids(rollback_connection, screen_id)
    now = datetime.now(UTC)
    started_showtime_id = await _insert_showtime(
        rollback_connection,
        movie_id=movie_id,
        screen_id=screen_id,
        starts_at=now,
        price_cents=800,
    )
    upcoming_showtime_id = await _insert_showtime(
        rollback_connection,
        movie_id=movie_id,
        screen_id=screen_id,
        starts_at=now + timedelta(seconds=1),
        price_cents=800,
    )
    user_id, _headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.started.user@example.com",
    )
    started_id = await _insert_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=started_showtime_id,
        seat_ids=[seat_ids[0]],
        status=_CONFIRMED,
        created_at=now - timedelta(minutes=5),
        confirmed_at=now - timedelta(minutes=4),
    )
    upcoming_id = await _insert_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=upcoming_showtime_id,
        seat_ids=[seat_ids[1]],
        status=_CONFIRMED,
        created_at=now - timedelta(minutes=5),
        confirmed_at=now - timedelta(minutes=4),
    )
    started_fields = await _stored_reservation_fields(
        rollback_connection,
        started_id,
    )
    before = await _reservation_row_counts(rollback_connection)

    with pytest.raises(ApiError) as raised:
        await service.cancel(rollback_connection, user_id, started_id, now)

    assert raised.value.code == "SHOWTIME_STARTED"
    assert raised.value.status_code == 409
    await _assert_rows_unchanged(rollback_connection, before)
    assert (
        await _stored_reservation_fields(rollback_connection, started_id)
    ) == started_fields
    await _assert_stored_status(rollback_connection, started_id, _CONFIRMED)

    cancelled = await service.cancel(
        rollback_connection,
        user_id,
        upcoming_id,
        now,
    )

    assert cancelled.id == upcoming_id
    assert cancelled.status == _CANCELLED
    assert cancelled.cancelled_at == now
    assert cancelled.expires_at is None
    assert cancelled.confirmed_at is not None
    assert cancelled.total_cents == 800
    assert [seat.seat_id for seat in cancelled.seats] == [seat_ids[1]]
    (
        status,
        _created_at,
        _confirmed_at,
        stored_cancelled_at,
    ) = await _stored_reservation_fields(rollback_connection, upcoming_id)
    assert status == _CANCELLED
    assert stored_cancelled_at == now
    await _assert_stored_status(rollback_connection, upcoming_id, _CANCELLED)
    await _assert_status_consistent(rollback_connection)


async def test_cancel_other_users_or_unknown_reservation_returns_404(
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
    """A foreign id looks exactly like an unknown one (FR-8, test 15).

    Both are 404 ``RESERVATION_NOT_FOUND`` and write nothing, so the endpoint
    never reveals whether another user's reservation exists.
    """
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.foreign.admin@example.com",
        title="Spec5 Foreign Movie",
        screen_name="Spec5 Foreign Screen",
        rows=1,
        seats_per_row=1,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _owner_id, owner_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.foreign.owner@example.com",
    )
    _other_id, other_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.foreign.other@example.com",
    )
    held = await _hold(client, owner_headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    confirmed = await _confirm(client, owner_headers, reservation_id)
    assert confirmed.status_code == 200, confirmed.text
    owner_fields = await _stored_reservation_fields(
        rollback_connection,
        reservation_id,
    )
    before = await _reservation_row_counts(rollback_connection)

    foreign = await _cancel(client, other_headers, reservation_id)

    _assert_error(foreign, 404, "RESERVATION_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)

    unknown = await _cancel(client, other_headers, _UNKNOWN_ID)

    _assert_error(unknown, 404, "RESERVATION_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)
    assert (
        await _stored_reservation_fields(rollback_connection, reservation_id)
    ) == owner_fields
    await _assert_stored_status(rollback_connection, reservation_id, _CONFIRMED)
    assert await _count_user_reservations(rollback_connection, _other_id) == 0


async def test_cancel_requires_authentication_returns_401(
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
    """Cancelling without credentials is a 401 that writes nothing (test 16).

    A real confirmed reservation exists, so the check cannot pass vacuously.
    """
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.cancel.auth.admin@example.com",
        title="Spec5 Cancel Auth Movie",
        screen_name="Spec5 Cancel Auth Screen",
        rows=1,
        seats_per_row=1,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.cancel.auth.user@example.com",
    )
    held = await _hold(client, headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    confirmed = await _confirm(client, headers, reservation_id)
    assert confirmed.status_code == 200, confirmed.text
    fields_before = await _stored_reservation_fields(
        rollback_connection,
        reservation_id,
    )
    before = await _reservation_row_counts(rollback_connection)

    response = await _cancel(client, None, reservation_id)

    _assert_error(response, 401, "UNAUTHENTICATED")
    await _assert_rows_unchanged(rollback_connection, before)
    assert (
        await _stored_reservation_fields(rollback_connection, reservation_id)
    ) == fields_before
    await _assert_stored_status(rollback_connection, reservation_id, _CONFIRMED)


async def test_cancel_leaves_other_reservations_of_same_showtime_untouched(
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
    """Cancelling one reservation leaves the showtime's others alone (test 17).

    Two users hold three confirmed reservations on one showtime; cancelling one
    must not touch the other two rows, their seat rows or their ``confirmed_at``.
    """
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.others.admin@example.com",
        title="Spec5 Others Movie",
        screen_name="Spec5 Others Screen",
        rows=1,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _first_id, first_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.others.first@example.com",
    )
    _second_id, second_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.others.second@example.com",
    )
    target_hold = await _hold(client, first_headers, showtime_id, [seat_ids[0]])
    assert target_hold.status_code == 201, target_hold.text
    target_id = int(_json_body(target_hold)["id"])
    assert (await _confirm(client, first_headers, target_id)).status_code == 200
    sibling_hold = await _hold(client, first_headers, showtime_id, [seat_ids[1]])
    assert sibling_hold.status_code == 201, sibling_hold.text
    sibling_id = int(_json_body(sibling_hold)["id"])
    assert (await _confirm(client, first_headers, sibling_id)).status_code == 200
    foreign_hold = await _hold(client, second_headers, showtime_id, [seat_ids[2]])
    assert foreign_hold.status_code == 201, foreign_hold.text
    foreign_id = int(_json_body(foreign_hold)["id"])
    assert (await _confirm(client, second_headers, foreign_id)).status_code == 200
    sibling_fields = await _stored_reservation_fields(
        rollback_connection,
        sibling_id,
    )
    foreign_fields = await _stored_reservation_fields(
        rollback_connection,
        foreign_id,
    )
    before = await _reservation_row_counts(rollback_connection)

    response = await _cancel(client, first_headers, target_id)

    assert response.status_code == 200, response.text
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, target_id, _CANCELLED)
    assert (
        await _stored_reservation_fields(rollback_connection, sibling_id)
    ) == sibling_fields
    assert (
        await _stored_reservation_fields(rollback_connection, foreign_id)
    ) == foreign_fields
    await _assert_stored_status(rollback_connection, sibling_id, _CONFIRMED)
    await _assert_stored_status(rollback_connection, foreign_id, _CONFIRMED)
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "available"
    assert states[seat_ids[1]] == "taken"
    assert states[seat_ids[2]] == "taken"
    await _assert_status_consistent(rollback_connection)


async def test_cancel_keeps_rows_and_list_shows_cancelled_status(
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
    """A cancelled reservation is history, not deleted (FR-11, test 18).

    The row survives, the list still returns it as ``cancelled`` with the cancel
    instant, and the ``status`` filter both includes and excludes it.
    """
    _admin, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.history.admin@example.com",
        title="Spec5 History Movie",
        screen_name="Spec5 History Screen",
        rows=1,
        seats_per_row=1,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.history.user@example.com",
    )
    held = await _hold(client, headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    assert (await _confirm(client, headers, reservation_id)).status_code == 200

    cancelled = await _cancel(client, headers, reservation_id)

    assert cancelled.status_code == 200, cancelled.text
    cancelled_at = _parse_optional_instant(_json_body(cancelled)["cancelled_at"])
    assert cancelled_at is not None
    items, total = _page(await _list(client, headers))
    assert total == 1
    item = _item(items, reservation_id)
    assert item["status"] == _CANCELLED
    assert _parse_optional_instant(item["cancelled_at"]) == cancelled_at
    assert _parse_optional_instant(item["confirmed_at"]) is not None
    assert item["expires_at"] is None
    cancelled_items, cancelled_total = _page(
        await _list(client, headers, {"status": "cancelled"})
    )
    assert cancelled_total == 1
    assert _ids(cancelled_items) == [reservation_id]
    confirmed_items, confirmed_total = _page(
        await _list(client, headers, {"status": "confirmed"})
    )
    assert confirmed_total == 0
    assert confirmed_items == []
    await _assert_stored_status(rollback_connection, reservation_id, _CANCELLED)


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
    """Every SPEC-5 failure uses the shared error envelope (test 19).

    The new ``RESERVATION_NOT_CONFIRMED`` is checked next to the existing
    ``RESERVATION_NOT_FOUND``, ``SHOWTIME_STARTED``, ``UNAUTHENTICATED`` and
    ``VALIDATION_ERROR``: same ``{"error": {"code", "message"}}`` shape, a
    non-empty distinct message and the documented status.
    """
    _admin, movie, screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec5.errors.admin@example.com",
        title="Spec5 Errors Movie",
        screen_name="Spec5 Errors Screen",
        rows=1,
        seats_per_row=2,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec5.errors.user@example.com",
    )
    held = await _hold(client, headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    held_id = int(_json_body(held)["id"])
    started_at = datetime.now(UTC) - timedelta(minutes=1)
    started_showtime_id = await _insert_showtime(
        rollback_connection,
        movie_id=int(movie["id"]),
        screen_id=int(screen["id"]),
        starts_at=started_at,
        price_cents=900,
    )
    started_id = await _insert_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=started_showtime_id,
        seat_ids=[seat_ids[1]],
        status=_CONFIRMED,
        created_at=started_at - timedelta(minutes=10),
        confirmed_at=started_at - timedelta(minutes=9),
    )
    before = await _reservation_row_counts(rollback_connection)

    failures = [
        (await _cancel(client, headers, held_id), 409, "RESERVATION_NOT_CONFIRMED"),
        (await _cancel(client, headers, _UNKNOWN_ID), 404, "RESERVATION_NOT_FOUND"),
        (await _cancel(client, headers, started_id), 409, "SHOWTIME_STARTED"),
        (await _cancel(client, None, held_id), 401, "UNAUTHENTICATED"),
        (await _list(client, headers, {"limit": 0}), 422, "VALIDATION_ERROR"),
    ]

    messages: list[str] = []
    for response, expected_status, expected_code in failures:
        assert response.status_code == expected_status, response.text
        error = _error_payload(response)
        assert set(error) == {"code", "message"}, error
        assert error["code"] == expected_code, error
        messages.append(error["message"])
    assert len(set(messages)) == len(messages), messages
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, held_id, _HELD)
    await _assert_stored_status(rollback_connection, started_id, _CONFIRMED)
