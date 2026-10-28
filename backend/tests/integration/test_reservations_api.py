"""Integration tests for the seat-map and reservation endpoints (SPEC-4 5).

Tests 6-34 drive the real FastAPI application over ASGI and the real Postgres
schema produced by the Alembic migration. Every test that needs data creates its
own screen, seats, movie, showtime and users; nothing relies on seeded rows.
Every test but the explicitly time-controlled ones shares one connection and one
transaction with the application, and that transaction is rolled back at
teardown, so no test commits.

Dates are always relative to ``datetime.now(UTC)``. Time-dependent tests call
the service functions directly with an explicit ``now`` so the hold-expiry
boundary can be exercised exactly at ``HOLD - 1 s``, ``HOLD`` and ``HOLD + 1 s``.
The two facts that cannot be set up through the API -- a started showtime and a
stored status -- are inserted or updated with SQL, as SPEC-4 Section 5 requires.

The shared SPEC-3 builders (administrator, movie, screen and showtime) live in
``tests/conftest.py`` and are injected here as fixtures. This module only adds
helpers specific to reservations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from src.core.config import get_settings
from src.core.errors import ApiError
from src.db.tables import reservations, seat_reservations
from src.reservations import service
from tests.conftest import (
    _CANCELLED,
    _CONFIRMED,
    _EXPIRED,
    _HELD,
    _RESERVATIONS_URL,
    _SEAT_MAP_TEMPLATE,
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
    _get_reservation,
    _hold,
    _insert_reservation,
    _insert_showtime,
    _json_body,
    _Login,
    _parse_optional_instant,
    _release,
    _reservation_row_counts,
    _screen_seat_ids,
    _seat_ids,
    _seat_map,
    _set_reservation_created_at,
    _states_by_seat,
    _stored_reservation_fields,
    _stored_reservation_status,
    _stored_seat_statuses,
)


async def test_seat_map_is_public_and_lists_every_seat_of_the_screen(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
) -> None:
    """The seat map needs no token and lists every seat in row order (FR-10, test 6)."""
    _headers, _movie, screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.map.public.admin@example.com",
        title="Spec4 Public Map Movie",
        screen_name="Spec4 Public Map Screen",
        rows=2,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])

    response = await client.get(
        _SEAT_MAP_TEMPLATE.format(showtime_id=showtime_id),
    )

    assert response.status_code == 200, response.text
    body = _json_body(response)
    assert set(body) == {"showtime_id", "screen", "seats"}, body
    assert body["showtime_id"] == showtime_id
    screen_body = body["screen"]
    assert isinstance(screen_body, dict), body
    assert set(screen_body) == {"id", "name", "rows", "seats_per_row"}, screen_body
    assert screen_body["id"] == screen["id"]
    assert screen_body["name"] == "Spec4 Public Map Screen"
    assert screen_body["rows"] == 2
    assert screen_body["seats_per_row"] == 3

    seats_body = body["seats"]
    assert isinstance(seats_body, list), body
    assert len(seats_body) == 6, seats_body
    assert all(
        set(seat) == {"seat_id", "row_label", "seat_number", "seat_type", "state"}
        for seat in seats_body
    ), seats_body
    assert [
        (str(seat["row_label"]), int(seat["seat_number"])) for seat in seats_body
    ] == [("A", 1), ("A", 2), ("A", 3), ("B", 1), ("B", 2), ("B", 3)]
    assert {int(seat["seat_id"]) for seat in seats_body} == set(
        await _screen_seat_ids(rollback_connection, int(screen["id"]))
    )
    assert {str(seat["seat_type"]) for seat in seats_body} == {"standard"}
    assert set(_states_by_seat(body).values()) == {"available"}
    assert "user" not in response.text.lower(), response.text


async def test_seat_map_shows_held_taken_and_treats_expired_hold_as_available(
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
    """The map derives held, taken and available (FR-10, test 7).

    A stored-but-stale hold derives ``available`` without being rewritten.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.map.states.admin@example.com",
        title="Spec4 Map States Movie",
        screen_name="Spec4 Map States Screen",
        rows=1,
        seats_per_row=5,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    held_user_id, held_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.map.states.held@example.com",
    )
    confirmed_user_id, _ = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.map.states.confirmed@example.com",
    )
    stale_user_id, _ = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.map.states.stale@example.com",
    )

    held_response = await _hold(
        client,
        held_headers,
        showtime_id,
        [seat_ids[0], seat_ids[1]],
    )
    assert held_response.status_code == 201, held_response.text
    now = datetime.now(UTC)
    await _insert_reservation(
        rollback_connection,
        user_id=confirmed_user_id,
        showtime_id=showtime_id,
        seat_ids=[seat_ids[2]],
        status=_CONFIRMED,
        created_at=now - timedelta(minutes=5),
        confirmed_at=now - timedelta(minutes=4),
    )
    stale_reservation_id = await _insert_reservation(
        rollback_connection,
        user_id=stale_user_id,
        showtime_id=showtime_id,
        seat_ids=[seat_ids[3]],
        status=_HELD,
        created_at=now
        - timedelta(minutes=get_settings().hold_minutes)
        - timedelta(seconds=1),
    )

    body = await _seat_map(client, showtime_id)
    states = _states_by_seat(body)

    assert states == {
        seat_ids[0]: "held",
        seat_ids[1]: "held",
        seat_ids[2]: "taken",
        seat_ids[3]: "available",
        seat_ids[4]: "available",
    }, states
    assert (
        await _stored_reservation_status(rollback_connection, stale_reservation_id)
        == _HELD
    )
    assert await _stored_seat_statuses(rollback_connection, stale_reservation_id) == [
        _HELD
    ]
    assert held_user_id > 0


async def test_seat_map_unknown_showtime_returns_404(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
) -> None:
    """An unknown showtime is a standard 404 and writes nothing (test 8)."""
    before = await _reservation_row_counts(rollback_connection)

    response = await client.get(_SEAT_MAP_TEMPLATE.format(showtime_id=_UNKNOWN_ID))

    _assert_error(response, 404, "SHOWTIME_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)


async def test_hold_requires_authentication_returns_401(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
) -> None:
    """A hold without a usable bearer token is 401 and writes nothing (test 9)."""
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.auth.admin@example.com",
        title="Spec4 Hold Auth Movie",
        screen_name="Spec4 Hold Auth Screen",
        rows=1,
        seats_per_row=2,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)

    for headers in (None, {"Authorization": "Bearer not-a-token"}):
        before = await _reservation_row_counts(rollback_connection)
        response = await client.post(
            _RESERVATIONS_URL,
            json={"showtime_id": showtime_id, "seat_ids": seat_ids},
            headers=headers,
        )
        _assert_error(response, 401, "UNAUTHENTICATED")
        assert response.headers["WWW-Authenticate"] == "Bearer"
        await _assert_rows_unchanged(rollback_connection, before)


async def test_hold_creates_held_reservation_with_seats_and_total(
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
    """A hold stores one held reservation and one held row per seat (FR-1, test 10)."""
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.create.admin@example.com",
        title="Spec4 Hold Create Movie",
        screen_name="Spec4 Hold Create Screen",
        rows=2,
        seats_per_row=3,
        price_cents=1_250,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.create.user@example.com",
    )

    response = await _hold(
        client,
        headers,
        showtime_id,
        [seat_ids[2], seat_ids[0]],
    )

    assert response.status_code == 201, response.text
    body = _json_body(response)
    assert set(body) == {
        "id",
        "showtime_id",
        "status",
        "seats",
        "total_cents",
        "created_at",
        "expires_at",
        "confirmed_at",
        "cancelled_at",
    }, body
    reservation_id = int(body["id"])
    assert reservation_id > 0
    assert body["showtime_id"] == showtime_id
    assert body["status"] == _HELD
    assert body["total_cents"] == 2_500
    seats_body = body["seats"]
    assert isinstance(seats_body, list), body
    assert all(
        set(seat) == {"seat_id", "row_label", "seat_number"} for seat in seats_body
    ), seats_body
    assert [
        (int(seat["seat_id"]), str(seat["row_label"]), int(seat["seat_number"]))
        for seat in seats_body
    ] == [(seat_ids[0], "A", 1), (seat_ids[2], "A", 3)]
    created_at = _parse_optional_instant(body["created_at"])
    expires_at = _parse_optional_instant(body["expires_at"])
    assert created_at is not None
    assert expires_at == created_at + timedelta(minutes=get_settings().hold_minutes)
    assert body["confirmed_at"] is None
    assert body["cancelled_at"] is None

    assert await _reservation_row_counts(rollback_connection) == (1, 2)
    await _assert_stored_status(rollback_connection, reservation_id, _HELD)
    assert await _stored_seat_statuses(rollback_connection, reservation_id) == [
        _HELD,
        _HELD,
    ]
    stored = await _stored_reservation_fields(rollback_connection, reservation_id)
    assert stored[0] == _HELD
    assert stored[1] == created_at
    assert stored[2] is None and stored[3] is None
    assert await _count_user_reservations(rollback_connection, user_id) == 1


async def test_hold_expires_at_is_created_plus_hold_minutes(
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
    """``expires_at`` is exactly ``created_at + HOLD_MINUTES`` (FR-5, test 11)."""
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.expires.admin@example.com",
        title="Spec4 Hold Expires Movie",
        screen_name="Spec4 Hold Expires Screen",
        rows=1,
        seats_per_row=2,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.expires.user@example.com",
    )

    response = await _hold(client, headers, showtime_id, [seat_ids[0]])

    assert response.status_code == 201, response.text
    body = _json_body(response)
    created_at = _parse_optional_instant(body["created_at"])
    expires_at = _parse_optional_instant(body["expires_at"])
    assert created_at is not None
    assert expires_at is not None
    assert expires_at - created_at == timedelta(minutes=get_settings().hold_minutes)
    assert created_at.utcoffset() == timedelta(0)
    assert expires_at.utcoffset() == timedelta(0)


async def test_hold_unknown_showtime_404_and_started_showtime_409(
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
    """An unknown showtime is 404; a started one is 409 (FR-4, test 12).

    Neither rejected request writes a reservation or seat row.
    """
    _headers, movie, screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.showtime.admin@example.com",
        title="Spec4 Hold Showtime Movie",
        screen_name="Spec4 Hold Showtime Screen",
        rows=1,
        seats_per_row=4,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.showtime.user@example.com",
    )

    before = await _reservation_row_counts(rollback_connection)
    unknown_response = await _hold(client, headers, _UNKNOWN_ID, [seat_ids[0]])
    _assert_error(unknown_response, 404, "SHOWTIME_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)

    started_showtime_id = await _insert_showtime(
        rollback_connection,
        movie_id=int(movie["id"]),
        screen_id=int(screen["id"]),
        starts_at=datetime.now(UTC) - timedelta(minutes=1),
        price_cents=900,
    )
    started_seat_ids = await _screen_seat_ids(
        rollback_connection,
        int(screen["id"]),
    )
    before = await _reservation_row_counts(rollback_connection)
    started_response = await _hold(
        client,
        headers,
        started_showtime_id,
        started_seat_ids[:2],
    )
    _assert_error(started_response, 409, "SHOWTIME_STARTED")
    await _assert_rows_unchanged(rollback_connection, before)


async def test_hold_seat_of_other_screen_or_unknown_returns_404_and_creates_nothing(
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
    """A foreign-screen or unknown seat id is 404 (FR-3, test 13).

    Both failures create no reservation and leave the requested seats free.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.seats.admin@example.com",
        title="Spec4 Hold Seats Movie",
        screen_name="Spec4 Hold Seats Screen",
        rows=1,
        seats_per_row=3,
    )
    (
        _other_headers,
        _other_movie,
        other_screen,
        _other_showtime,
    ) = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.other.admin@example.com",
        title="Spec4 Hold Other Movie",
        screen_name="Spec4 Hold Other Screen",
        rows=1,
        seats_per_row=3,
        start_offset=timedelta(days=3),
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    other_seat_ids = await _screen_seat_ids(
        rollback_connection, int(other_screen["id"])
    )
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.seats.user@example.com",
    )

    before = await _reservation_row_counts(rollback_connection)
    foreign_response = await _hold(
        client,
        headers,
        showtime_id,
        [seat_ids[0], other_seat_ids[0]],
    )
    _assert_error(foreign_response, 404, "SEAT_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)

    unknown_response = await _hold(
        client,
        headers,
        showtime_id,
        [seat_ids[0], _UNKNOWN_ID],
    )
    _assert_error(unknown_response, 404, "SEAT_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)

    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "available"
    assert states[seat_ids[1]] == "available"
    assert states[seat_ids[2]] == "available"


async def test_hold_validation_failures_return_422(
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
    """Every malformed hold is 422 (FR-3, test 14).

    The seat cap accepts exactly ``MAX_SEATS_PER_HOLD`` seats and rejects one
    more, so both sides of the boundary are pinned.
    """
    settings = get_settings()
    max_seats = settings.max_seats_per_hold
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.validation.admin@example.com",
        title="Spec4 Hold Validation Movie",
        screen_name="Spec4 Hold Validation Screen",
        rows=1,
        seats_per_row=max(5, max_seats + 1),
        price_cents=2_000,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.validation.user@example.com",
    )
    cases: list[tuple[dict[str, Any], str]] = [
        ({"showtime_id": showtime_id, "seat_ids": []}, "VALIDATION_ERROR"),
        (
            {"showtime_id": showtime_id, "seat_ids": [seat_ids[0], seat_ids[0]]},
            "VALIDATION_ERROR",
        ),
        (
            {
                "showtime_id": showtime_id,
                "seat_ids": [seat_ids[0]],
                "unexpected": True,
            },
            "VALIDATION_ERROR",
        ),
        (
            {"showtime_id": showtime_id, "seat_ids": [str(seat_ids[0])]},
            "VALIDATION_ERROR",
        ),
        (
            {"showtime_id": showtime_id, "seat_ids": seat_ids[: max_seats + 1]},
            "TOO_MANY_SEATS",
        ),
    ]

    for payload, code in cases:
        before = await _reservation_row_counts(rollback_connection)
        response = await client.post(
            _RESERVATIONS_URL,
            json=payload,
            headers=headers,
        )
        _assert_error(response, 422, code)
        await _assert_rows_unchanged(rollback_connection, before)

    accepted = await client.post(
        _RESERVATIONS_URL,
        json={"showtime_id": showtime_id, "seat_ids": seat_ids[:max_seats]},
        headers=headers,
    )

    assert accepted.status_code == 201, accepted.text
    body = _json_body(accepted)
    seats_body = body["seats"]
    assert isinstance(seats_body, list), body
    assert len(seats_body) == max_seats
    assert body["total_cents"] == 2_000 * max_seats
    await _assert_stored_status(rollback_connection, int(body["id"]), _HELD)


async def test_hold_with_taken_seat_returns_409_all_or_nothing(
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
    """A conflicting seat makes the whole hold fail, naming the seat (FR-2, test 15)."""
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.conflict.admin@example.com",
        title="Spec4 Hold Conflict Movie",
        screen_name="Spec4 Hold Conflict Screen",
        rows=1,
        seats_per_row=4,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_a_id, user_a_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.conflict.a@example.com",
    )
    user_b_id, user_b_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.conflict.b@example.com",
    )

    held = await _hold(
        client,
        user_a_headers,
        showtime_id,
        [seat_ids[0], seat_ids[1]],
    )
    assert held.status_code == 201, held.text

    before = await _reservation_row_counts(rollback_connection)
    response = await _hold(
        client,
        user_b_headers,
        showtime_id,
        [seat_ids[1], seat_ids[2]],
    )
    _assert_error(response, 409, "SEAT_UNAVAILABLE")
    assert f"Seat {seat_ids[1]}" in _error_payload(response)["message"]
    await _assert_rows_unchanged(rollback_connection, before)
    assert await _count_user_reservations(rollback_connection, user_b_id) == 0

    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "held"
    assert states[seat_ids[1]] == "held"
    assert states[seat_ids[2]] == "available"
    assert states[seat_ids[3]] == "available"


async def test_hold_of_confirmed_seat_returns_409(
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
    """A confirmed seat is unavailable to a later hold (FR-2, test 16).

    The refusal names the seat and writes no reservation row.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.confirmed.admin@example.com",
        title="Spec4 Hold Confirmed Movie",
        screen_name="Spec4 Hold Confirmed Screen",
        rows=1,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    confirmed_user_id, _ = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.confirmed.owner@example.com",
    )
    user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.confirmed.other@example.com",
    )
    now = datetime.now(UTC)
    confirmed_id = await _insert_reservation(
        rollback_connection,
        user_id=confirmed_user_id,
        showtime_id=showtime_id,
        seat_ids=[seat_ids[0]],
        status=_CONFIRMED,
        created_at=now - timedelta(minutes=5),
        confirmed_at=now - timedelta(minutes=4),
    )

    before = await _reservation_row_counts(rollback_connection)
    response = await _hold(client, headers, showtime_id, [seat_ids[0]])

    _assert_error(response, 409, "SEAT_UNAVAILABLE")
    assert str(seat_ids[0]) in _error_payload(response)["message"]
    await _assert_rows_unchanged(rollback_connection, before)
    assert await _count_user_reservations(rollback_connection, user_id) == 0
    await _assert_stored_status(rollback_connection, confirmed_id, _CONFIRMED)
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "taken"


async def test_release_frees_seats_and_same_user_can_hold_again(
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
    """Releasing frees the seats for the same user (FR-6, test 17).

    The released reservation is stored as ``cancelled`` and a new hold succeeds
    on the same seats.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.release.rehold.admin@example.com",
        title="Spec4 Release Rehold Movie",
        screen_name="Spec4 Release Rehold Screen",
        rows=1,
        seats_per_row=3,
        price_cents=1_100,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.release.rehold.user@example.com",
    )
    held = await _hold(
        client,
        headers,
        showtime_id,
        [seat_ids[0], seat_ids[1]],
    )
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    before = await _reservation_row_counts(rollback_connection)

    released = await _release(client, headers, reservation_id)

    assert released.status_code == 200, released.text
    body = _json_body(released)
    assert body["id"] == reservation_id
    assert body["status"] == _CANCELLED
    assert body["total_cents"] == 2_200
    assert body["expires_at"] is None
    assert _parse_optional_instant(body["cancelled_at"]) is not None
    assert body["confirmed_at"] is None
    seats_body = body["seats"]
    assert isinstance(seats_body, list), body
    assert [int(seat["seat_id"]) for seat in seats_body] == [
        seat_ids[0],
        seat_ids[1],
    ]
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, reservation_id, _CANCELLED)
    assert await _stored_seat_statuses(rollback_connection, reservation_id) == [
        _CANCELLED,
        _CANCELLED,
    ]
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "available"
    assert states[seat_ids[1]] == "available"

    reheld = await _hold(
        client,
        headers,
        showtime_id,
        [seat_ids[0], seat_ids[1]],
    )
    assert reheld.status_code == 201, reheld.text
    reheld_body = _json_body(reheld)
    assert int(reheld_body["id"]) != reservation_id
    assert reheld_body["status"] == _HELD
    assert await _count_user_reservations(rollback_connection, user_id) == 2
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "held"
    assert states[seat_ids[1]] == "held"


async def test_stale_hold_is_treated_as_available_when_new_hold_attempted(
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
    """An expired hold is freed lazily on a new hold attempt (FR-5, test 18).

    The stale reservation and all its seat rows are persisted as ``expired``
    before the competing hold proceeds.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.stale.admin@example.com",
        title="Spec4 Hold Stale Movie",
        screen_name="Spec4 Hold Stale Screen",
        rows=1,
        seats_per_row=2,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_a_id, user_a_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.stale.a@example.com",
    )
    _user_b_id, user_b_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.stale.b@example.com",
    )
    stale_hold = await _hold(client, user_a_headers, showtime_id, [seat_ids[0]])
    assert stale_hold.status_code == 201, stale_hold.text
    stale_id = int(_json_body(stale_hold)["id"])
    await _set_reservation_created_at(
        rollback_connection,
        stale_id,
        datetime.now(UTC)
        - timedelta(minutes=get_settings().hold_minutes)
        - timedelta(seconds=1),
    )

    response = await _hold(client, user_b_headers, showtime_id, [seat_ids[0]])

    assert response.status_code == 201, response.text
    new_id = int(_json_body(response)["id"])
    await _assert_stored_status(rollback_connection, stale_id, _EXPIRED)
    assert await _stored_seat_statuses(rollback_connection, stale_id) == [_EXPIRED]
    await _assert_stored_status(rollback_connection, new_id, _HELD)
    assert await _reservation_row_counts(rollback_connection) == (2, 2)
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "held"


async def test_hold_expiry_is_exactly_at_expires_at(
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
    """The seat frees exactly at ``expires_at``, one second later (FR-5, test 19)."""
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.expiry.boundary.admin@example.com",
        title="Spec4 Expiry Boundary Movie",
        screen_name="Spec4 Expiry Boundary Screen",
        rows=1,
        seats_per_row=2,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    user_a_id, _ = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.expiry.boundary.a@example.com",
    )
    user_b_id, _ = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.expiry.boundary.b@example.com",
    )
    hold_minutes = get_settings().hold_minutes
    base = datetime.now(UTC)

    stale = await service.hold(
        rollback_connection,
        user_a_id,
        showtime_id,
        [seat_ids[0]],
        base,
    )
    assert stale.status == _HELD

    with pytest.raises(ApiError) as before_expiry:
        await service.hold(
            rollback_connection,
            user_b_id,
            showtime_id,
            [seat_ids[0]],
            base + timedelta(minutes=hold_minutes) - timedelta(seconds=1),
        )
    assert before_expiry.value.code == "SEAT_UNAVAILABLE"
    await _assert_stored_status(rollback_connection, int(stale.id), _HELD)

    replacement = await service.hold(
        rollback_connection,
        user_b_id,
        showtime_id,
        [seat_ids[0]],
        base + timedelta(minutes=hold_minutes),
    )

    assert replacement.status == _HELD
    await _assert_stored_status(rollback_connection, int(stale.id), _EXPIRED)
    assert await _stored_seat_statuses(rollback_connection, int(stale.id)) == [_EXPIRED]
    await _assert_stored_status(rollback_connection, int(replacement.id), _HELD)
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "held"


async def test_one_active_hold_per_user_per_showtime_allowed_after_expiry_or_release(
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
    """A user has one active hold per showtime (FR-6, test 20).

    Another showtime is independent, and release or expiry frees the user to
    hold again on the same showtime.
    """
    headers, movie, screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.active.hold.admin@example.com",
        title="Spec4 Active Hold Movie",
        screen_name="Spec4 Active Hold Screen",
        rows=1,
        seats_per_row=4,
        price_cents=1_000,
    )
    showtime_id = int(showtime["id"])
    second_showtime = await create_showtime_via_api(
        client,
        headers,
        int(movie["id"]),
        int(screen["id"]),
        future_start(timedelta(days=2)),
        1_000,
    )
    second_showtime_id = int(second_showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    user_id, user_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.active.hold.user@example.com",
    )

    first = await _hold(client, user_headers, showtime_id, [seat_ids[0]])
    assert first.status_code == 201, first.text
    first_id = int(_json_body(first)["id"])

    before = await _reservation_row_counts(rollback_connection)
    conflict = await _hold(client, user_headers, showtime_id, [seat_ids[1]])
    _assert_error(conflict, 409, "ACTIVE_HOLD_EXISTS")
    await _assert_rows_unchanged(rollback_connection, before)

    other_showtime = await _hold(
        client,
        user_headers,
        second_showtime_id,
        [seat_ids[0]],
    )
    assert other_showtime.status_code == 201, other_showtime.text
    other_showtime_id_of_hold = int(_json_body(other_showtime)["id"])

    released = await _release(client, user_headers, first_id)
    assert released.status_code == 200, released.text
    reheld = await _hold(client, user_headers, showtime_id, [seat_ids[1]])
    assert reheld.status_code == 201, reheld.text
    reheld_id = int(_json_body(reheld)["id"])

    await _set_reservation_created_at(
        rollback_connection,
        other_showtime_id_of_hold,
        datetime.now(UTC)
        - timedelta(minutes=get_settings().hold_minutes)
        - timedelta(seconds=1),
    )
    after_expiry = await _hold(
        client,
        user_headers,
        second_showtime_id,
        [seat_ids[1]],
    )

    assert after_expiry.status_code == 201, after_expiry.text
    after_expiry_id = int(_json_body(after_expiry)["id"])
    await _assert_stored_status(
        rollback_connection, other_showtime_id_of_hold, _EXPIRED
    )
    await _assert_stored_status(rollback_connection, reheld_id, _HELD)
    await _assert_stored_status(rollback_connection, after_expiry_id, _HELD)
    assert await _count_user_reservations(rollback_connection, user_id) == 4
    assert await _reservation_row_counts(rollback_connection) == (4, 4)


async def test_lazy_expiry_only_affects_stale_holds_of_the_target_showtime(
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
    """Lazy expiry only touches the target showtime's stale holds (FR-5, test 21)."""
    headers, movie, screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.lazy.scope.admin@example.com",
        title="Spec4 Lazy Scope Movie",
        screen_name="Spec4 Lazy Scope Screen",
        rows=1,
        seats_per_row=3,
        price_cents=1_000,
    )
    showtime_id = int(showtime["id"])
    second_showtime = await create_showtime_via_api(
        client,
        headers,
        int(movie["id"]),
        int(screen["id"]),
        future_start(timedelta(days=2)),
        1_000,
    )
    second_showtime_id = int(second_showtime["id"])
    first_seats = await _seat_ids(client, showtime_id)
    second_seats = await _seat_ids(client, second_showtime_id)
    _user_a_id, user_a_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.lazy.scope.a@example.com",
    )
    _user_b_id, user_b_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.lazy.scope.b@example.com",
    )
    _user_c_id, user_c_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.lazy.scope.c@example.com",
    )
    stale = await _hold(client, user_a_headers, showtime_id, [first_seats[0]])
    assert stale.status_code == 201, stale.text
    stale_id = int(_json_body(stale)["id"])
    other = await _hold(client, user_b_headers, second_showtime_id, [second_seats[1]])
    assert other.status_code == 201, other.text
    other_id = int(_json_body(other)["id"])
    await _set_reservation_created_at(
        rollback_connection,
        stale_id,
        datetime.now(UTC)
        - timedelta(minutes=get_settings().hold_minutes)
        - timedelta(seconds=1),
    )

    response = await _hold(
        client,
        user_c_headers,
        showtime_id,
        [first_seats[1]],
    )

    assert response.status_code == 201, response.text
    await _assert_stored_status(rollback_connection, stale_id, _EXPIRED)
    await _assert_stored_status(rollback_connection, other_id, _HELD)
    assert await _stored_seat_statuses(rollback_connection, other_id) == [_HELD]
    states = _states_by_seat(await _seat_map(client, second_showtime_id))
    assert states[second_seats[1]] == "held"


async def test_confirm_held_reservation_marks_it_confirmed(
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
    """Confirming a held reservation stores ``confirmed`` (FR-7, test 22).

    The seats stay attached, ``expires_at`` is cleared and the map reports them
    as taken.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.confirm.success.admin@example.com",
        title="Spec4 Confirm Success Movie",
        screen_name="Spec4 Confirm Success Screen",
        rows=1,
        seats_per_row=3,
        price_cents=1_200,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.confirm.success.user@example.com",
    )
    held = await _hold(
        client,
        headers,
        showtime_id,
        [seat_ids[2], seat_ids[0]],
    )
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    before = await _reservation_row_counts(rollback_connection)

    response = await _confirm(client, headers, reservation_id)

    assert response.status_code == 200, response.text
    body = _json_body(response)
    assert set(body) == {
        "id",
        "showtime_id",
        "status",
        "seats",
        "total_cents",
        "created_at",
        "expires_at",
        "confirmed_at",
        "cancelled_at",
    }, body
    assert body["id"] == reservation_id
    assert body["showtime_id"] == showtime_id
    assert body["status"] == _CONFIRMED
    assert body["total_cents"] == 2_400
    assert body["expires_at"] is None
    assert body["cancelled_at"] is None
    confirmed_at = _parse_optional_instant(body["confirmed_at"])
    assert confirmed_at is not None
    seats_body = body["seats"]
    assert isinstance(seats_body, list), body
    assert [int(seat["seat_id"]) for seat in seats_body] == [
        seat_ids[0],
        seat_ids[2],
    ]
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, reservation_id, _CONFIRMED)
    assert await _stored_seat_statuses(rollback_connection, reservation_id) == [
        _CONFIRMED,
        _CONFIRMED,
    ]
    stored = await _stored_reservation_fields(rollback_connection, reservation_id)
    assert stored[0] == _CONFIRMED
    assert stored[2] == confirmed_at
    assert stored[3] is None
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "taken"
    assert states[seat_ids[2]] == "taken"
    assert states[seat_ids[1]] == "available"


async def test_confirm_confirmed_reservation_is_idempotent(
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
    """A second confirm returns the same representation (D7, test 23).

    It writes no reservation or seat row and keeps the original
    ``confirmed_at``.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.confirm.idempotent.admin@example.com",
        title="Spec4 Confirm Idempotent Movie",
        screen_name="Spec4 Confirm Idempotent Screen",
        rows=1,
        seats_per_row=2,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.confirm.idempotent.user@example.com",
    )
    held = await _hold(client, headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    first = await _confirm(client, headers, reservation_id)
    assert first.status_code == 200, first.text
    first_body = _json_body(first)
    before = await _reservation_row_counts(rollback_connection)

    second = await _confirm(client, headers, reservation_id)

    assert second.status_code == 200, second.text
    assert _json_body(second) == first_body
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, reservation_id, _CONFIRMED)
    assert await _stored_seat_statuses(rollback_connection, reservation_id) == [
        _CONFIRMED
    ]


async def test_confirm_expiry_boundary_and_no_write_on_expired(
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
    """Confirm succeeds before expiry and fails exactly at it (FR-7, test 24).

    The failing reservation stays ``held`` with no ``confirmed_at`` and no row
    is rewritten.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.confirm.boundary.admin@example.com",
        title="Spec4 Confirm Boundary Movie",
        screen_name="Spec4 Confirm Boundary Screen",
        rows=1,
        seats_per_row=2,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    user_a_id, _ = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.confirm.boundary.a@example.com",
    )
    user_b_id, _ = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.confirm.boundary.b@example.com",
    )
    hold_minutes = get_settings().hold_minutes
    base = datetime.now(UTC)
    held_a = await service.hold(
        rollback_connection,
        user_a_id,
        showtime_id,
        [seat_ids[0]],
        base,
    )
    held_b = await service.hold(
        rollback_connection,
        user_b_id,
        showtime_id,
        [seat_ids[1]],
        base,
    )
    before = await _reservation_row_counts(rollback_connection)

    confirmed = await service.confirm(
        rollback_connection,
        user_a_id,
        int(held_a.id),
        base + timedelta(minutes=hold_minutes) - timedelta(seconds=1),
    )
    assert confirmed.status == _CONFIRMED
    assert confirmed.confirmed_at == (
        base + timedelta(minutes=hold_minutes) - timedelta(seconds=1)
    )

    with pytest.raises(ApiError) as expired:
        await service.confirm(
            rollback_connection,
            user_b_id,
            int(held_b.id),
            base + timedelta(minutes=hold_minutes),
        )
    assert expired.value.code == "RESERVATION_EXPIRED"
    assert expired.value.status_code == 409
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, int(held_b.id), _HELD)
    stored = await _stored_reservation_fields(rollback_connection, int(held_b.id))
    assert stored[2] is None
    await _assert_stored_status(rollback_connection, int(held_a.id), _CONFIRMED)


async def test_confirm_released_then_expired_reservation_returns_409(
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
    """Confirm refuses cancelled and expired reservations (FR-7, test 25).

    Both cases are ``RESERVATION_NOT_HELD`` and neither rewrites any row.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.confirm.notheld.admin@example.com",
        title="Spec4 Confirm Not Held Movie",
        screen_name="Spec4 Confirm Not Held Screen",
        rows=1,
        seats_per_row=3,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    user_a_id, user_a_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.confirm.notheld.a@example.com",
    )
    user_b_id, user_b_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.confirm.notheld.b@example.com",
    )
    held = await _hold(client, user_a_headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    cancelled_id = int(_json_body(held)["id"])
    released = await _release(client, user_a_headers, cancelled_id)
    assert released.status_code == 200, released.text
    before = await _reservation_row_counts(rollback_connection)

    cancelled_confirm = await _confirm(client, user_a_headers, cancelled_id)

    _assert_error(cancelled_confirm, 409, "RESERVATION_NOT_HELD")
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, cancelled_id, _CANCELLED)

    expired_id = await _insert_reservation(
        rollback_connection,
        user_id=user_b_id,
        showtime_id=showtime_id,
        seat_ids=[seat_ids[1]],
        status=_EXPIRED,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    before = await _reservation_row_counts(rollback_connection)
    expired_confirm = await _confirm(client, user_b_headers, expired_id)

    _assert_error(expired_confirm, 409, "RESERVATION_NOT_HELD")
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, expired_id, _EXPIRED)
    assert await _count_user_reservations(rollback_connection, user_a_id) == 1


async def test_confirm_started_showtime_returns_409(
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
    """Confirm refuses a hold whose showtime has started (FR-7, test 26).

    The reservation stays ``held`` and its seat remains unavailable.
    """
    _headers, movie, screen, _showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.confirm.started.admin@example.com",
        title="Spec4 Confirm Started Movie",
        screen_name="Spec4 Confirm Started Screen",
        rows=1,
        seats_per_row=2,
    )
    started_showtime_id = await _insert_showtime(
        rollback_connection,
        movie_id=int(movie["id"]),
        screen_id=int(screen["id"]),
        starts_at=datetime.now(UTC) - timedelta(hours=1),
        price_cents=800,
    )
    started_seats = await _screen_seat_ids(
        rollback_connection,
        int(screen["id"]),
    )
    user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.confirm.started.user@example.com",
    )
    reservation_id = await _insert_reservation(
        rollback_connection,
        user_id=user_id,
        showtime_id=started_showtime_id,
        seat_ids=[started_seats[0]],
        status=_HELD,
        created_at=datetime.now(UTC),
    )

    before = await _reservation_row_counts(rollback_connection)
    response = await _confirm(client, headers, reservation_id)

    _assert_error(response, 409, "SHOWTIME_STARTED")
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, reservation_id, _HELD)
    states = _states_by_seat(await _seat_map(client, started_showtime_id))
    assert states[started_seats[0]] == "held"


async def test_confirm_another_users_reservation_returns_404(
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
    """Confirm is owner-scoped: a foreign id is 404 (FR-7, test 27).

    The request writes nothing and the owner's reservation stays ``held``.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.confirm.owner.admin@example.com",
        title="Spec4 Confirm Owner Movie",
        screen_name="Spec4 Confirm Owner Screen",
        rows=1,
        seats_per_row=2,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_a_id, user_a_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.confirm.owner.a@example.com",
    )
    _user_b_id, user_b_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.confirm.owner.b@example.com",
    )
    held = await _hold(client, user_a_headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    before = await _reservation_row_counts(rollback_connection)

    foreign = await _confirm(client, user_b_headers, reservation_id)
    _assert_error(foreign, 404, "RESERVATION_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)

    unknown = await _confirm(client, user_b_headers, _UNKNOWN_ID)
    _assert_error(unknown, 404, "RESERVATION_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, reservation_id, _HELD)


async def test_hold_after_confirm_is_rejected_as_seat_unavailable(
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
    """A confirmed seat stays unavailable to every other hold (FR-2, test 28)."""
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.hold.after.confirm.admin@example.com",
        title="Spec4 Hold After Confirm Movie",
        screen_name="Spec4 Hold After Confirm Screen",
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
        "spec4.hold.after.confirm.owner@example.com",
    )
    user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.hold.after.confirm.other@example.com",
    )
    held = await _hold(client, owner_headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    confirmed = await _confirm(client, owner_headers, reservation_id)
    assert confirmed.status_code == 200, confirmed.text

    before = await _reservation_row_counts(rollback_connection)
    response = await _hold(client, headers, showtime_id, [seat_ids[0]])

    _assert_error(response, 409, "SEAT_UNAVAILABLE")
    await _assert_rows_unchanged(rollback_connection, before)
    assert await _count_user_reservations(rollback_connection, user_id) == 0
    await _assert_stored_status(rollback_connection, reservation_id, _CONFIRMED)
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "taken"


async def test_release_returns_cancelled_and_frees_seats_for_another_user(
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
    """Release returns ``cancelled`` and frees its seats (FR-8, test 29).

    Another user can immediately hold the same seats.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.release.frees.admin@example.com",
        title="Spec4 Release Frees Movie",
        screen_name="Spec4 Release Frees Screen",
        rows=1,
        seats_per_row=3,
        price_cents=1_500,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _owner_id, owner_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.release.frees.owner@example.com",
    )
    _other_id, other_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.release.frees.other@example.com",
    )
    held = await _hold(
        client,
        owner_headers,
        showtime_id,
        [seat_ids[2], seat_ids[0]],
    )
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])

    response = await _release(client, owner_headers, reservation_id)

    assert response.status_code == 200, response.text
    body = _json_body(response)
    assert body["id"] == reservation_id
    assert body["status"] == _CANCELLED
    assert body["total_cents"] == 3_000
    assert body["expires_at"] is None
    assert body["confirmed_at"] is None
    assert _parse_optional_instant(body["cancelled_at"]) is not None
    seats_body = body["seats"]
    assert isinstance(seats_body, list), body
    assert [int(seat["seat_id"]) for seat in seats_body] == [
        seat_ids[0],
        seat_ids[2],
    ]
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "available"
    assert states[seat_ids[2]] == "available"
    await _assert_stored_status(rollback_connection, reservation_id, _CANCELLED)

    reheld = await _hold(
        client,
        other_headers,
        showtime_id,
        [seat_ids[0], seat_ids[2]],
    )

    assert reheld.status_code == 201, reheld.text
    assert _json_body(reheld)["status"] == _HELD
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "held"
    assert states[seat_ids[2]] == "held"


async def test_release_confirmed_returns_409_and_other_users_returns_404(
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
    """Release refuses a confirmed reservation or a foreign id (FR-8, test 30).

    The failed requests write nothing and the reservation stays ``confirmed``.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.release.notheld.admin@example.com",
        title="Spec4 Release Not Held Movie",
        screen_name="Spec4 Release Not Held Screen",
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
        "spec4.release.notheld.owner@example.com",
    )
    _other_id, other_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.release.notheld.other@example.com",
    )
    held = await _hold(client, owner_headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])
    confirmed = await _confirm(client, owner_headers, reservation_id)
    assert confirmed.status_code == 200, confirmed.text
    before = await _reservation_row_counts(rollback_connection)

    not_held = await _release(client, owner_headers, reservation_id)
    _assert_error(not_held, 409, "RESERVATION_NOT_HELD")
    await _assert_rows_unchanged(rollback_connection, before)

    foreign = await _release(client, other_headers, reservation_id)
    _assert_error(foreign, 404, "RESERVATION_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)

    unknown = await _release(client, other_headers, _UNKNOWN_ID)
    _assert_error(unknown, 404, "RESERVATION_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, reservation_id, _CONFIRMED)


async def test_get_reservation_returns_held_then_expired_and_hides_foreign(
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
    """GET reports held then expired; foreign and unknown ids are 404 (FR-9, test 31).

    Reading an aged held reservation derives ``expired`` without rewriting the
    stored row, and its seat becomes available.
    """
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.get.reservation.admin@example.com",
        title="Spec4 Get Reservation Movie",
        screen_name="Spec4 Get Reservation Screen",
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
        "spec4.get.reservation.owner@example.com",
    )
    _other_id, other_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.get.reservation.other@example.com",
    )
    held = await _hold(client, owner_headers, showtime_id, [seat_ids[0]])
    assert held.status_code == 201, held.text
    reservation_id = int(_json_body(held)["id"])

    response = await _get_reservation(client, owner_headers, reservation_id)

    assert response.status_code == 200, response.text
    body = _json_body(response)
    assert body["id"] == reservation_id
    assert body["status"] == _HELD
    assert _parse_optional_instant(body["expires_at"]) is not None
    before = await _reservation_row_counts(rollback_connection)

    foreign = await _get_reservation(client, other_headers, reservation_id)
    _assert_error(foreign, 404, "RESERVATION_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)
    unknown = await _get_reservation(client, other_headers, _UNKNOWN_ID)
    _assert_error(unknown, 404, "RESERVATION_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)

    await _set_reservation_created_at(
        rollback_connection,
        reservation_id,
        datetime.now(UTC)
        - timedelta(minutes=get_settings().hold_minutes)
        - timedelta(seconds=1),
    )
    expired = await _get_reservation(client, owner_headers, reservation_id)

    assert expired.status_code == 200, expired.text
    expired_body = _json_body(expired)
    assert expired_body["status"] == _EXPIRED
    assert expired_body["expires_at"] is None
    assert expired_body["confirmed_at"] is None
    assert expired_body["cancelled_at"] is None
    await _assert_rows_unchanged(rollback_connection, before)
    await _assert_stored_status(rollback_connection, reservation_id, _HELD)
    states = _states_by_seat(await _seat_map(client, showtime_id))
    assert states[seat_ids[0]] == "available"


async def test_total_cents_is_price_times_seats_across_lifecycle(
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
    """``total_cents`` stays ``price * seats`` across the lifecycle (FR-11, test 32).

    The value is computed on hold, read back by GET and preserved by confirm.
    """
    price_cents = 1_375
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.total.admin@example.com",
        title="Spec4 Total Movie",
        screen_name="Spec4 Total Screen",
        rows=2,
        seats_per_row=3,
        price_cents=price_cents,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_id, headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.total.user@example.com",
    )
    requested = [seat_ids[0], seat_ids[1], seat_ids[4]]

    held = await _hold(client, headers, showtime_id, requested)
    assert held.status_code == 201, held.text
    held_body = _json_body(held)
    reservation_id = int(held_body["id"])
    expected_total = price_cents * len(requested)
    assert held_body["total_cents"] == expected_total

    fetched = await _get_reservation(client, headers, reservation_id)
    assert fetched.status_code == 200, fetched.text
    assert _json_body(fetched)["total_cents"] == expected_total

    confirmed = await _confirm(client, headers, reservation_id)
    assert confirmed.status_code == 200, confirmed.text
    confirmed_body = _json_body(confirmed)
    assert confirmed_body["total_cents"] == expected_total
    assert len(confirmed_body["seats"]) == len(requested)


async def test_standard_error_shape_for_new_codes(
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
    """Every SPEC-4 error code uses the standard ``error`` shape (test 33)."""
    settings = get_settings()
    max_seats = settings.max_seats_per_hold
    _headers, movie, screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.errors.admin@example.com",
        title="Spec4 Errors Movie",
        screen_name="Spec4 Errors Screen",
        rows=1,
        seats_per_row=max(5, max_seats + 1),
        price_cents=1_000,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_a_id, user_a_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.errors.a@example.com",
    )
    _user_b_id, user_b_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.errors.b@example.com",
    )
    _user_c_id, user_c_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.errors.c@example.com",
    )

    before = await _reservation_row_counts(rollback_connection)
    unauthenticated = await client.post(
        _RESERVATIONS_URL,
        json={"showtime_id": showtime_id, "seat_ids": [seat_ids[0]]},
    )
    _assert_error(unauthenticated, 401, "UNAUTHENTICATED")
    assert unauthenticated.headers["WWW-Authenticate"] == "Bearer"
    await _assert_rows_unchanged(rollback_connection, before)

    before = await _reservation_row_counts(rollback_connection)
    missing_showtime = await client.get(
        _SEAT_MAP_TEMPLATE.format(showtime_id=_UNKNOWN_ID)
    )
    _assert_error(missing_showtime, 404, "SHOWTIME_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)

    validation_cases: list[dict[str, Any]] = [
        {"showtime_id": showtime_id, "seat_ids": []},
        {"showtime_id": showtime_id, "seat_ids": [seat_ids[0], seat_ids[0]]},
    ]
    for payload in validation_cases:
        before = await _reservation_row_counts(rollback_connection)
        response = await client.post(
            _RESERVATIONS_URL,
            json=payload,
            headers=user_a_headers,
        )
        _assert_error(response, 422, "VALIDATION_ERROR")
        await _assert_rows_unchanged(rollback_connection, before)

    before = await _reservation_row_counts(rollback_connection)
    too_many = await _hold(
        client,
        user_a_headers,
        showtime_id,
        seat_ids[: max_seats + 1],
    )
    _assert_error(too_many, 422, "TOO_MANY_SEATS")
    await _assert_rows_unchanged(rollback_connection, before)

    before = await _reservation_row_counts(rollback_connection)
    missing_seat = await _hold(
        client,
        user_a_headers,
        showtime_id,
        [seat_ids[0], _UNKNOWN_ID],
    )
    _assert_error(missing_seat, 404, "SEAT_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)

    before = await _reservation_row_counts(rollback_connection)
    missing_reservation = await _get_reservation(
        client,
        user_a_headers,
        _UNKNOWN_ID,
    )
    _assert_error(missing_reservation, 404, "RESERVATION_NOT_FOUND")
    await _assert_rows_unchanged(rollback_connection, before)

    first_hold = await _hold(client, user_a_headers, showtime_id, [seat_ids[0]])
    assert first_hold.status_code == 201, first_hold.text
    before = await _reservation_row_counts(rollback_connection)
    active_hold = await _hold(client, user_a_headers, showtime_id, [seat_ids[1]])
    _assert_error(active_hold, 409, "ACTIVE_HOLD_EXISTS")
    await _assert_rows_unchanged(rollback_connection, before)

    before = await _reservation_row_counts(rollback_connection)
    seat_unavailable = await _hold(
        client,
        user_b_headers,
        showtime_id,
        [seat_ids[0]],
    )
    _assert_error(seat_unavailable, 409, "SEAT_UNAVAILABLE")
    await _assert_rows_unchanged(rollback_connection, before)

    expired_id = await _insert_reservation(
        rollback_connection,
        user_id=_user_c_id,
        showtime_id=showtime_id,
        seat_ids=[seat_ids[2]],
        status=_HELD,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    before = await _reservation_row_counts(rollback_connection)
    expired_confirm = await _confirm(client, user_c_headers, expired_id)
    _assert_error(expired_confirm, 409, "RESERVATION_EXPIRED")
    await _assert_rows_unchanged(rollback_connection, before)

    cancelled_id = await _insert_reservation(
        rollback_connection,
        user_id=_user_c_id,
        showtime_id=showtime_id,
        seat_ids=[seat_ids[3]],
        status=_CANCELLED,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
        cancelled_at=datetime.now(UTC) - timedelta(minutes=4),
    )
    before = await _reservation_row_counts(rollback_connection)
    not_held = await _confirm(client, user_c_headers, cancelled_id)
    _assert_error(not_held, 409, "RESERVATION_NOT_HELD")
    await _assert_rows_unchanged(rollback_connection, before)

    started_showtime_id = await _insert_showtime(
        rollback_connection,
        movie_id=int(movie["id"]),
        screen_id=int(screen["id"]),
        starts_at=datetime.now(UTC) - timedelta(minutes=30),
        price_cents=1_000,
    )
    started_seat_ids = await _screen_seat_ids(
        rollback_connection,
        int(screen["id"]),
    )
    before = await _reservation_row_counts(rollback_connection)
    started = await _hold(
        client,
        user_c_headers,
        started_showtime_id,
        [started_seat_ids[4]],
    )
    _assert_error(started, 409, "SHOWTIME_STARTED")
    await _assert_rows_unchanged(rollback_connection, before)


async def test_every_status_transition_keeps_reservation_and_seat_rows_consistent(
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
    """A reservation and all its seat rows always share one status (test 34)."""
    _headers, _movie, _screen, showtime = await _create_screening(
        client,
        rollback_connection,
        admin_headers,
        future_start,
        create_movie_via_api,
        create_screen_via_api,
        create_showtime_via_api,
        email="spec4.consistency.admin@example.com",
        title="Spec4 Consistency Movie",
        screen_name="Spec4 Consistency Screen",
        rows=1,
        seats_per_row=5,
    )
    showtime_id = int(showtime["id"])
    seat_ids = await _seat_ids(client, showtime_id)
    _user_a_id, user_a_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.consistency.a@example.com",
    )
    _user_b_id, user_b_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.consistency.b@example.com",
    )
    _user_c_id, user_c_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.consistency.c@example.com",
    )
    _user_d_id, user_d_headers = await _create_account(
        client,
        create_user,
        login,
        auth_header,
        "spec4.consistency.d@example.com",
    )

    held_a = await _hold(client, user_a_headers, showtime_id, [seat_ids[0]])
    assert held_a.status_code == 201, held_a.text
    held_a_id = int(_json_body(held_a)["id"])
    await _assert_status_consistent(rollback_connection)

    confirmed_a = await _confirm(client, user_a_headers, held_a_id)
    assert confirmed_a.status_code == 200, confirmed_a.text
    await _assert_status_consistent(rollback_connection)

    held_b = await _hold(client, user_b_headers, showtime_id, [seat_ids[1]])
    assert held_b.status_code == 201, held_b.text
    held_b_id = int(_json_body(held_b)["id"])
    await _assert_status_consistent(rollback_connection)

    released_b = await _release(client, user_b_headers, held_b_id)
    assert released_b.status_code == 200, released_b.text
    await _assert_status_consistent(rollback_connection)

    held_c = await _hold(client, user_c_headers, showtime_id, [seat_ids[2]])
    assert held_c.status_code == 201, held_c.text
    held_c_id = int(_json_body(held_c)["id"])
    await _set_reservation_created_at(
        rollback_connection,
        held_c_id,
        datetime.now(UTC)
        - timedelta(minutes=get_settings().hold_minutes)
        - timedelta(seconds=1),
    )
    await _assert_status_consistent(rollback_connection)

    held_d = await _hold(client, user_d_headers, showtime_id, [seat_ids[2]])
    assert held_d.status_code == 201, held_d.text
    await _assert_status_consistent(rollback_connection)

    statuses = set(
        (
            await rollback_connection.execute(select(reservations.c.status).distinct())
        ).scalars()
    )
    assert statuses == {_HELD, _CONFIRMED, _CANCELLED, _EXPIRED}, statuses
    orphans = (
        await rollback_connection.execute(
            select(reservations.c.id)
            .select_from(
                reservations.outerjoin(
                    seat_reservations,
                    seat_reservations.c.reservation_id == reservations.c.id,
                )
            )
            .where(seat_reservations.c.id.is_(None))
        )
    ).all()
    assert orphans == [], orphans
    assert await _reservation_row_counts(rollback_connection) == (4, 4)
