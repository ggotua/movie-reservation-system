"""Concurrency integration tests for the reservation lifecycle (SPEC-5 5).

Tests 20-21 exercise the real FastAPI application and the real PostgreSQL schema
through the ``committing_client`` fixture. Unlike the rolled-back tests in
``test_reservation_lifecycle_api.py``, every request here gets its own
connection and its own transaction, because the two scenarios only exist when
independent transactions compete for the same reservation row or the same seat
row.

Each test creates its own users, movie, screens with seats and showtimes, and
removes every row it created in a ``finally`` block: reservations first (their
seat rows cascade), then showtimes, seats, the screens, the movies and the
users. A failed assertion therefore cannot leave committed rows behind for the
next run.

The competing requests are released together through an ``asyncio.Event`` that
the last ready task sets and are then awaited with ``asyncio.gather``, so they
overlap in the event loop and in PostgreSQL instead of being serialised by test
setup.

Test 21 does not assume which interleaving happens. Both are correct: the hold
either conflicts with the still-active confirmed seat rows and is refused with
409 ``SEAT_UNAVAILABLE``, or it runs after the cancel committed and succeeds
with 201. Only the invariants are asserted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import httpx
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.core.config import get_settings
from src.core.enums import ACTIVE_RESERVATION_STATUSES, DEFAULT_USER_ROLE
from src.core.security import create_access_token, hash_password
from src.db.tables import (
    movies,
    reservations,
    screens,
    seat_reservations,
    seats,
    showtimes,
    users,
)

_RESERVATIONS_URL = "/reservations"
_SEAT_UNAVAILABLE = "SEAT_UNAVAILABLE"
_RESERVATION_NOT_CONFIRMED = "RESERVATION_NOT_CONFIRMED"
_CANCELLED = "cancelled"
_CONFIRMED = "confirmed"

# Ten rounds for test 21, as specified in SPEC-5 Section 5.
_ROUNDS = 10

# Ordinary throwaway test data, never a credential (IMPLEMENTER RULES, rule 6).
_USER_PASSWORD = "spec-5 concurrency test password"

_Account = tuple[int, dict[str, str]]
_Screening = tuple[int, int, int, list[int]]
_RequestFactory = Callable[[], Awaitable[httpx.Response]]


def _json_body(response: httpx.Response) -> dict[str, Any]:
    """Return a response body as a JSON object.

    Args:
        response: The response to read.

    Returns:
        dict[str, Any]: The parsed body.

    Raises:
        AssertionError: If the body is not a JSON object, with it attached.
    """
    body: Any = response.json()
    assert isinstance(body, dict), body
    return body


def _error_code(response: httpx.Response) -> str:
    """Return the machine-readable error code of a failure response.

    Args:
        response: The failure response.

    Returns:
        str: The stable ``SCREAMING_SNAKE_CASE`` code from the standard
        ``{"error": {"code", "message"}}`` body.

    Raises:
        AssertionError: If the body does not have the standard error shape.
    """
    body = _json_body(response)
    assert set(body) == {"error"}, body
    error = body["error"]
    assert isinstance(error, dict), body
    assert set(error) == {"code", "message"}, body
    assert isinstance(error["code"], str) and error["code"], body
    assert isinstance(error["message"], str) and error["message"], body
    return str(error["code"])


async def _hold(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    showtime_id: int,
    seat_ids: Sequence[int],
) -> httpx.Response:
    """Post a hold request and return its raw response.

    Args:
        client: The ASGI client bound to the committing application.
        headers: The acting user's ``Authorization`` header.
        showtime_id: The showtime to hold seats on.
        seat_ids: The requested seat ids.

    Returns:
        httpx.Response: The unasserted response.
    """
    return await client.post(
        _RESERVATIONS_URL,
        json={"showtime_id": showtime_id, "seat_ids": list(seat_ids)},
        headers=headers,
    )


async def _confirm(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    reservation_id: int,
) -> httpx.Response:
    """Post a confirmation request and return its raw response.

    Args:
        client: The ASGI client bound to the committing application.
        headers: The owner's ``Authorization`` header.
        reservation_id: The reservation to confirm.

    Returns:
        httpx.Response: The unasserted response.
    """
    return await client.post(
        f"{_RESERVATIONS_URL}/{reservation_id}/confirm",
        headers=headers,
    )


async def _cancel(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    reservation_id: int,
) -> httpx.Response:
    """Post a cancel request and return its raw response.

    Args:
        client: The ASGI client bound to the committing application.
        headers: The owner's ``Authorization`` header.
        reservation_id: The confirmed reservation to cancel.

    Returns:
        httpx.Response: The unasserted response.
    """
    return await client.post(
        f"{_RESERVATIONS_URL}/{reservation_id}/cancel",
        headers=headers,
    )


async def _gather_after_barrier(
    requests: Sequence[_RequestFactory],
) -> list[httpx.Response]:
    """Run request factories together, released by one start barrier.

    Every task increments a ready counter under a lock and then waits on one
    ``asyncio.Event``. The task that completes the count sets the event, so the
    requests are released as a group instead of one after another; the tasks are
    then awaited together with ``asyncio.gather``.

    Args:
        requests: Zero-argument callables that each start one HTTP request.
            Must not be empty.

    Returns:
        list[httpx.Response]: The responses, in the same order as ``requests``.
    """
    assert requests, "at least one request is required"
    start = asyncio.Event()
    ready_count = 0
    ready_lock = asyncio.Lock()

    async def _run(request: _RequestFactory) -> httpx.Response:
        """Mark readiness, wait for the group release, then send the request."""
        nonlocal ready_count
        async with ready_lock:
            ready_count += 1
            if ready_count == len(requests):
                start.set()
        await start.wait()
        return await request()

    tasks = [asyncio.create_task(_run(request)) for request in requests]
    return list(await asyncio.gather(*tasks))


async def _create_users(
    engine: AsyncEngine,
    emails: Sequence[str],
) -> list[_Account]:
    """Insert committed users and return their ids with bearer headers.

    The tokens are signed with the application's own settings, so the requests
    below authenticate exactly like a real client would; no login request and no
    password-storage dependency is needed.

    Args:
        engine: The committing test engine.
        emails: Unique, already-normalized email addresses.

    Returns:
        list[_Account]: ``(user_id, Authorization header)`` in ``emails`` order.
    """
    password_hash = hash_password(_USER_PASSWORD)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                insert(users)
                .values(
                    [
                        {
                            "email": email,
                            "password_hash": password_hash,
                            "role": DEFAULT_USER_ROLE,
                        }
                        for email in emails
                    ]
                )
                .returning(users.c.id, users.c.email)
            )
        ).all()

    ids_by_email = {str(row.email): int(row.id) for row in rows}
    settings = get_settings()
    accounts: list[_Account] = []
    for email in emails:
        user_id = ids_by_email[email]
        token = create_access_token(
            user_id,
            settings.jwt_secret,
            settings.jwt_expiry_minutes,
        )
        accounts.append((user_id, {"Authorization": f"Bearer {token}"}))
    return accounts


async def _create_screening(
    engine: AsyncEngine,
    *,
    slug: str,
    seats_per_row: int,
    price_cents: int = 1_000,
) -> _Screening:
    """Insert a committed movie, screen, seats and future showtime.

    Args:
        engine: The committing test engine.
        slug: Unique text used in the movie title and screen name.
        seats_per_row: Number of seats to create in the screen's single row.
        price_cents: Ticket price in integer cents.

    Returns:
        _Screening: ``(movie_id, screen_id, showtime_id, seat_ids)``, with seat
        ids ordered by seat number.
    """
    starts_at = datetime.now(UTC) + timedelta(days=2)
    async with engine.begin() as conn:
        movie_id = int(
            (
                await conn.execute(
                    insert(movies)
                    .values(title=f"SPEC-5 concurrency {slug}")
                    .returning(movies.c.id)
                )
            ).scalar_one()
        )
        screen_id = int(
            (
                await conn.execute(
                    insert(screens)
                    .values(
                        name=f"SPEC-5 concurrency screen {slug}",
                        rows=1,
                        seats_per_row=seats_per_row,
                    )
                    .returning(screens.c.id)
                )
            ).scalar_one()
        )
        seat_rows = (
            await conn.execute(
                insert(seats)
                .values(
                    [
                        {
                            "screen_id": screen_id,
                            "row_label": "A",
                            "seat_number": seat_number,
                        }
                        for seat_number in range(1, seats_per_row + 1)
                    ]
                )
                .returning(seats.c.id, seats.c.seat_number)
            )
        ).all()
        showtime_id = int(
            (
                await conn.execute(
                    insert(showtimes)
                    .values(
                        movie_id=movie_id,
                        screen_id=screen_id,
                        starts_at=starts_at,
                        price_cents=price_cents,
                    )
                    .returning(showtimes.c.id)
                )
            ).scalar_one()
        )

    seat_ids_by_number = {int(row.seat_number): int(row.id) for row in seat_rows}
    seat_ids = [seat_ids_by_number[number] for number in range(1, seats_per_row + 1)]
    return movie_id, screen_id, showtime_id, seat_ids


async def _remove_test_data(
    engine: AsyncEngine,
    *,
    user_ids: Sequence[int] = (),
    showtime_ids: Sequence[int] = (),
    screen_ids: Sequence[int] = (),
    movie_ids: Sequence[int] = (),
) -> None:
    """Delete every committed row a concurrency test created.

    Deletion order follows the foreign keys: reservations first (their
    ``seat_reservations`` rows cascade), then showtimes, seats, screens, movies
    and finally the users. Every sequence is optional so cleanup also works when
    setup failed part-way through.

    Args:
        engine: The committing test engine.
        user_ids: User ids created by the test.
        showtime_ids: Showtime ids created by the test.
        screen_ids: Screen ids created by the test.
        movie_ids: Movie ids created by the test.
    """
    async with engine.begin() as conn:
        if user_ids:
            await conn.execute(
                delete(reservations).where(reservations.c.user_id.in_(user_ids))
            )
        if showtime_ids:
            await conn.execute(
                delete(reservations).where(reservations.c.showtime_id.in_(showtime_ids))
            )
            await conn.execute(
                delete(showtimes).where(showtimes.c.id.in_(showtime_ids))
            )
        if screen_ids:
            await conn.execute(delete(seats).where(seats.c.screen_id.in_(screen_ids)))
            await conn.execute(delete(screens).where(screens.c.id.in_(screen_ids)))
        if movie_ids:
            await conn.execute(delete(movies).where(movies.c.id.in_(movie_ids)))
        if user_ids:
            await conn.execute(delete(users).where(users.c.id.in_(user_ids)))


async def _stored_status(conn: AsyncConnection, reservation_id: int) -> str:
    """Read the stored status of one reservation.

    Args:
        conn: An open read connection.
        reservation_id: The reservation to inspect.

    Returns:
        str: The stored status, not the API's effective status.
    """
    return str(
        (
            await conn.execute(
                select(reservations.c.status).where(reservations.c.id == reservation_id)
            )
        ).scalar_one()
    )


async def _seat_row_statuses(
    conn: AsyncConnection,
    reservation_id: int,
) -> list[str]:
    """Read the stored statuses of one reservation's seat rows.

    Args:
        conn: An open read connection.
        reservation_id: The reservation whose seat rows to inspect.

    Returns:
        list[str]: One status per seat row, ordered by seat id.
    """
    rows = (
        await conn.execute(
            select(seat_reservations.c.status)
            .where(seat_reservations.c.reservation_id == reservation_id)
            .order_by(seat_reservations.c.seat_id)
        )
    ).all()
    return [str(row.status) for row in rows]


async def _active_reservation_ids_for_seat(
    conn: AsyncConnection,
    seat_id: int,
) -> list[int]:
    """Read the reservations that currently hold one seat.

    Args:
        conn: An open read connection.
        seat_id: The seat to inspect.

    Returns:
        list[int]: The reservation id of every ``held`` or ``confirmed`` seat
        row for that seat; empty when the seat is free. After the cancel of
        test 21 this is the invariant the whole test exists for: never more than
        one row.
    """
    rows = (
        await conn.execute(
            select(seat_reservations.c.reservation_id)
            .where(
                seat_reservations.c.seat_id == seat_id,
                seat_reservations.c.status.in_(ACTIVE_RESERVATION_STATUSES),
            )
            .order_by(seat_reservations.c.reservation_id)
        )
    ).all()
    return [int(row.reservation_id) for row in rows]


async def _create_confirmed_reservation(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    showtime_id: int,
    seat_ids: Sequence[int],
) -> int:
    """Hold and confirm seats through the API, returning the reservation id.

    Args:
        client: The ASGI client bound to the committing application.
        headers: The owner's ``Authorization`` header.
        showtime_id: The showtime to book.
        seat_ids: The seats to book.

    Returns:
        int: The confirmed reservation's primary key.

    Raises:
        AssertionError: If the hold is not 201 or the confirm is not 200, with
        the body attached.
    """
    hold = await _hold(client, headers, showtime_id, seat_ids)
    assert hold.status_code == 201, (hold.status_code, hold.text)
    reservation_id = int(_json_body(hold)["id"])

    confirmed = await _confirm(client, headers, reservation_id)
    assert confirmed.status_code == 200, (confirmed.status_code, confirmed.text)
    assert _json_body(confirmed)["status"] == _CONFIRMED, confirmed.text
    return reservation_id


async def test_concurrent_double_cancel_exactly_one_succeeds(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """Two cancels of one confirmed reservation: exactly one wins (FR-9, test 20).

    Both requests are released together. The reservation row lock serialises
    them, so the winner answers 200 and the loser sees ``cancelled`` and answers
    409 ``RESERVATION_NOT_CONFIRMED`` -- cancel is deliberately not idempotent
    (D2). Nothing may answer 5xx. Afterwards the stored reservation and every
    one of its seat rows must be ``cancelled`` (FR-6, FR-10), and the seats must
    no longer be active.
    """
    user_ids: list[int] = []
    movie_id: int | None = None
    screen_id: int | None = None
    showtime_id: int | None = None

    try:
        accounts = await _create_users(
            async_engine, ["spec5.concurrent.20.owner@example.com"]
        )
        user_ids = [user_id for user_id, _headers in accounts]
        owner_headers = accounts[0][1]
        movie_id, screen_id, showtime_id, seat_ids = await _create_screening(
            async_engine,
            slug="test-20",
            seats_per_row=2,
        )

        reservation_id = await _create_confirmed_reservation(
            committing_client,
            owner_headers,
            showtime_id,
            seat_ids,
        )

        responses = await _gather_after_barrier(
            [partial(_cancel, committing_client, owner_headers, reservation_id)] * 2
        )

        assert all(response.status_code < 500 for response in responses), [
            (response.status_code, response.text) for response in responses
        ]
        statuses = sorted(response.status_code for response in responses)
        assert statuses == [200, 409], [
            (response.status_code, response.text) for response in responses
        ]

        winner = next(response for response in responses if response.status_code == 200)
        assert _json_body(winner)["status"] == _CANCELLED, winner.text

        loser = next(response for response in responses if response.status_code == 409)
        assert _error_code(loser) == _RESERVATION_NOT_CONFIRMED, loser.text

        async with async_engine.connect() as conn:
            stored_status = await _stored_status(conn, reservation_id)
            seat_statuses = await _seat_row_statuses(conn, reservation_id)
            active_rows = [
                await _active_reservation_ids_for_seat(conn, seat_id)
                for seat_id in seat_ids
            ]

        assert stored_status == _CANCELLED, stored_status
        assert seat_statuses == [_CANCELLED] * len(seat_ids), seat_statuses
        assert active_rows == [[] for _seat_id in seat_ids], active_rows
    finally:
        await _remove_test_data(
            async_engine,
            user_ids=user_ids,
            showtime_ids=[] if showtime_id is None else [showtime_id],
            screen_ids=[] if screen_id is None else [screen_id],
            movie_ids=[] if movie_id is None else [movie_id],
        )


async def test_concurrent_cancel_and_hold_of_same_seat_never_two_active_rows(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """Cancel racing a new hold of the same seat keeps the index invariant (FR-9).

    The test runs ten rounds, each with fresh data: the owner cancels a
    confirmed reservation while another user concurrently holds the same seat.
    Both interleavings are correct and neither is assumed:

    - the hold arrives first and conflicts with the still-active confirmed seat
      row, so it answers 409 ``SEAT_UNAVAILABLE``; or
    - the hold runs after the cancel committed, so it answers 201 and owns the
      seat.

    Per round the hold is 201 or 409 and never 5xx, the cancel is 200, the seat
    has at most one active ``seat_reservations`` row afterwards, and the original
    reservation is ``cancelled`` with all of its seat rows ``cancelled``
    (FR-6, FR-10).
    """
    user_ids: list[int] = []
    showtime_ids: list[int] = []
    screen_ids: list[int] = []
    movie_ids: list[int] = []

    try:
        accounts = await _create_users(
            async_engine,
            [
                "spec5.concurrent.21.owner@example.com",
                "spec5.concurrent.21.rival@example.com",
            ],
        )
        user_ids = [user_id for user_id, _headers in accounts]
        owner_headers = accounts[0][1]
        rival_headers = accounts[1][1]

        for round_index in range(_ROUNDS):
            movie_id, screen_id, showtime_id, seat_ids = await _create_screening(
                async_engine,
                slug=f"test-21-round-{round_index}",
                seats_per_row=1,
            )
            movie_ids.append(movie_id)
            screen_ids.append(screen_id)
            showtime_ids.append(showtime_id)
            seat_id = seat_ids[0]

            reservation_id = await _create_confirmed_reservation(
                committing_client,
                owner_headers,
                showtime_id,
                seat_ids,
            )

            cancel_response, hold_response = await _gather_after_barrier(
                [
                    partial(_cancel, committing_client, owner_headers, reservation_id),
                    partial(
                        _hold,
                        committing_client,
                        rival_headers,
                        showtime_id,
                        [seat_id],
                    ),
                ]
            )

            context = (
                round_index,
                cancel_response.status_code,
                cancel_response.text,
                hold_response.status_code,
                hold_response.text,
            )
            assert cancel_response.status_code == 200, context
            assert hold_response.status_code in (201, 409), context
            if hold_response.status_code == 409:
                assert _error_code(hold_response) == _SEAT_UNAVAILABLE, context

            async with async_engine.connect() as conn:
                active_ids = await _active_reservation_ids_for_seat(conn, seat_id)
                stored_status = await _stored_status(conn, reservation_id)
                seat_statuses = await _seat_row_statuses(conn, reservation_id)

            assert len(active_ids) <= 1, (context, active_ids)
            assert stored_status == _CANCELLED, (context, stored_status)
            assert seat_statuses == [_CANCELLED], (context, seat_statuses)

            if hold_response.status_code == 201:
                new_reservation_id = int(_json_body(hold_response)["id"])
                assert active_ids == [new_reservation_id], (context, active_ids)
            else:
                assert active_ids == [], (context, active_ids)
    finally:
        await _remove_test_data(
            async_engine,
            user_ids=user_ids,
            showtime_ids=showtime_ids,
            screen_ids=screen_ids,
            movie_ids=movie_ids,
        )
