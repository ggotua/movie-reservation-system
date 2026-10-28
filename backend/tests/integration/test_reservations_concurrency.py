"""Concurrency integration tests for the seat-reservation engine (SPEC-4 5).

Tests 35-40 exercise the real FastAPI application and the real PostgreSQL schema
through the ``committing_client`` fixture. Unlike the rolled-back tests in
``test_reservations_api.py``, every request here gets its own connection and its
own transaction, because a concurrency test must let independent transactions
compete for the same seat rows.

Each test creates its own users, movie, screen with seats and showtime, and
removes every row it created in a ``finally`` block: reservations first (their
seat rows cascade), then showtimes, seats, the screen, the movie and the users.
A failed assertion therefore cannot leave committed rows behind for the next
run.

Every competing request is released together through an ``asyncio.Event`` that
the last ready task sets. The requests therefore overlap in the event loop and
in PostgreSQL instead of being serialised by test setup.

The committing engine uses SQLAlchemy's default pool (five connections plus ten
overflow connections). Twenty to thirty requests may therefore queue briefly,
but the tests below deliberately do not reduce that number: if the pool were too
small for the load, the requests would time out and the test would fail rather
than silently changing the scenario.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import httpx
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.core.config import get_settings
from src.core.enums import (
    ACTIVE_RESERVATION_STATUSES,
    DEFAULT_RESERVATION_STATUS,
    DEFAULT_USER_ROLE,
)
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
_ACTIVE_HOLD_EXISTS = "ACTIVE_HOLD_EXISTS"
_RESERVATION_EXPIRED = "RESERVATION_EXPIRED"

# Ordinary throwaway test data, never a credential (IMPLEMENTER RULES, rule 6).
_USER_PASSWORD = "spec-4 concurrency test password"

_Account = tuple[int, dict[str, str]]
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


def _error_payload(response: httpx.Response) -> dict[str, str]:
    """Return the standard ``error`` object of a failure response.

    Args:
        response: The failure response.

    Returns:
        dict[str, str]: The ``code`` and ``message`` of the failure.

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
    return error


def _error_code(response: httpx.Response) -> str:
    """Return the machine-readable error code of a failure response.

    Args:
        response: The failure response.

    Returns:
        str: The stable ``SCREAMING_SNAKE_CASE`` code.
    """
    return _error_payload(response)["code"]


async def _hold(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    showtime_id: int,
    seat_ids: Sequence[int],
) -> httpx.Response:
    """Post a hold request and return its raw response.

    Args:
        client: The ASGI client bound to the application under test.
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
        client: The ASGI client bound to the application under test.
        headers: The acting user's ``Authorization`` header.
        reservation_id: The reservation to confirm.

    Returns:
        httpx.Response: The unasserted response.
    """
    return await client.post(
        f"{_RESERVATIONS_URL}/{reservation_id}/confirm",
        headers=headers,
    )


async def _start_after_barrier(
    requests: Sequence[_RequestFactory],
) -> list[asyncio.Task[httpx.Response]]:
    """Create request tasks, release them together, and return them running.

    Every task increments a ready counter under a lock and then waits on one
    ``asyncio.Event``. The task that makes the count complete sets the event, so
    the requests are released as one group rather than one after another.

    Args:
        requests: Zero-argument callables that each start one HTTP request.
            They must be non-empty.

    Returns:
        list[asyncio.Task[httpx.Response]]: The running request tasks, in the
        same order as ``requests``.
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
    await start.wait()
    return tasks


async def _gather_after_barrier(
    requests: Sequence[_RequestFactory],
) -> list[httpx.Response]:
    """Run request factories together after all of them are ready.

    Args:
        requests: Zero-argument callables that each start one HTTP request.
            They must be non-empty.

    Returns:
        list[httpx.Response]: The responses in the same order as ``requests``
        (``asyncio.gather`` preserves argument order).
    """
    tasks = await _start_after_barrier(requests)
    responses = await asyncio.gather(*tasks)
    return list(responses)


async def _create_users(
    engine: AsyncEngine,
    emails: Sequence[str],
) -> list[_Account]:
    """Insert committed user rows and mint one bearer header per account.

    The accounts are created directly rather than through signup: these tests
    exercise the reservation transaction, and bcrypt work for thirty accounts
    would only slow the gate. The JWT is minted with the same
    :func:`src.core.security.create_access_token` the login route uses, so the
    application authenticates the accounts through its normal dependency.

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
) -> tuple[int, int, int, list[int]]:
    """Insert a committed movie, screen, seats and future showtime.

    Args:
        engine: The committing test engine.
        slug: Unique text used in the movie title and screen name.
        seats_per_row: Number of seats to create in the screen's single row.
        price_cents: Ticket price in integer cents.

    Returns:
        tuple[int, int, int, list[int]]: ``(movie_id, screen_id, showtime_id,
        seat_ids)``, with seat ids ordered by seat number.
    """
    starts_at = datetime.now(UTC) + timedelta(days=2)
    async with engine.begin() as conn:
        movie_id = int(
            (
                await conn.execute(
                    insert(movies)
                    .values(title=f"SPEC-4 concurrency {slug}")
                    .returning(movies.c.id)
                )
            ).scalar_one()
        )
        screen_id = int(
            (
                await conn.execute(
                    insert(screens)
                    .values(
                        name=f"SPEC-4 concurrency screen {slug}",
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
    showtime_id: int | None = None,
    screen_id: int | None = None,
    movie_id: int | None = None,
) -> None:
    """Delete every committed row a concurrency test created.

    Deletion order follows the foreign keys: reservations first (their
    ``seat_reservations`` rows cascade), then showtimes, seats, the screen, the
    movie and finally the users. Each id is optional so cleanup still works when
    setup failed part-way through.

    Args:
        engine: The committing test engine.
        user_ids: User ids created by the test.
        showtime_id: The test's showtime id, if one was created.
        screen_id: The test's screen id, if one was created.
        movie_id: The test's movie id, if one was created.
    """
    async with engine.begin() as conn:
        if user_ids:
            await conn.execute(
                delete(reservations).where(reservations.c.user_id.in_(user_ids))
            )
        if showtime_id is not None:
            await conn.execute(
                delete(reservations).where(reservations.c.showtime_id == showtime_id)
            )
            await conn.execute(delete(showtimes).where(showtimes.c.id == showtime_id))
        if screen_id is not None:
            await conn.execute(delete(seats).where(seats.c.screen_id == screen_id))
            await conn.execute(delete(screens).where(screens.c.id == screen_id))
        if movie_id is not None:
            await conn.execute(delete(movies).where(movies.c.id == movie_id))
        if user_ids:
            await conn.execute(delete(users).where(users.c.id.in_(user_ids)))


async def _active_seat_pairs(
    conn: AsyncConnection,
    showtime_id: int,
) -> list[tuple[int, int]]:
    """Read every active seat row of one showtime.

    Args:
        conn: An open read connection.
        showtime_id: The showtime to inspect.

    Returns:
        list[tuple[int, int]]: ``(reservation_id, seat_id)`` for every ``held``
        or ``confirmed`` seat row, ordered by seat id.
    """
    rows = (
        await conn.execute(
            select(
                seat_reservations.c.reservation_id,
                seat_reservations.c.seat_id,
            )
            .where(
                seat_reservations.c.showtime_id == showtime_id,
                seat_reservations.c.status.in_(ACTIVE_RESERVATION_STATUSES),
            )
            .order_by(seat_reservations.c.seat_id)
        )
    ).all()
    return [(int(row.reservation_id), int(row.seat_id)) for row in rows]


async def _reservation_ids_for_users(
    conn: AsyncConnection,
    user_ids: Sequence[int],
) -> list[int]:
    """Read the reservation ids owned by a set of users.

    Args:
        conn: An open read connection.
        user_ids: The accounts whose reservations to list.

    Returns:
        list[int]: Reservation ids in ascending order; empty when no user owns
        a reservation.
    """
    rows = (
        await conn.execute(
            select(reservations.c.id)
            .where(reservations.c.user_id.in_(user_ids))
            .order_by(reservations.c.id)
        )
    ).all()
    return [int(row.id) for row in rows]


async def _reservation_seat_ids(
    conn: AsyncConnection,
    reservation_id: int,
) -> list[int]:
    """Read the stored seat ids of one reservation.

    Args:
        conn: An open read connection.
        reservation_id: The reservation to inspect.

    Returns:
        list[int]: Seat ids in ascending order.
    """
    rows = (
        await conn.execute(
            select(seat_reservations.c.seat_id)
            .where(seat_reservations.c.reservation_id == reservation_id)
            .order_by(seat_reservations.c.seat_id)
        )
    ).all()
    return [int(row.seat_id) for row in rows]


async def _stored_status(
    conn: AsyncConnection,
    reservation_id: int,
) -> str:
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


async def _stored_seat_statuses(
    conn: AsyncConnection,
    reservation_id: int,
) -> list[str]:
    """Read the stored statuses of one reservation's seat rows.

    Args:
        conn: An open read connection.
        reservation_id: The reservation to inspect.

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


async def test_concurrent_holds_of_one_seat_exactly_one_succeeds(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """Twenty users race for one seat: exactly one wins (FR-2, test 35).

    The winner gets 201; every other request gets 409 ``SEAT_UNAVAILABLE``.
    PostgreSQL must contain exactly one active seat row and exactly one
    reservation row for the showtime.
    """
    user_ids: list[int] = []
    movie_id: int | None = None
    screen_id: int | None = None
    showtime_id: int | None = None

    try:
        emails = [f"spec4.concurrent.35.{index}@example.com" for index in range(20)]
        accounts = await _create_users(async_engine, emails)
        user_ids = [user_id for user_id, _headers in accounts]
        movie_id, screen_id, showtime_id, seat_ids = await _create_screening(
            async_engine,
            slug="test-35",
            seats_per_row=1,
        )
        seat_id = seat_ids[0]

        responses = await _gather_after_barrier(
            [
                partial(_hold, committing_client, headers, showtime_id, [seat_id])
                for _user_id, headers in accounts
            ]
        )

        statuses = sorted(response.status_code for response in responses)
        assert statuses == [201] + [409] * 19, [
            (response.status_code, response.text) for response in responses
        ]
        for response in responses:
            if response.status_code == 409:
                assert _error_code(response) == _SEAT_UNAVAILABLE, response.text

        success = next(
            response for response in responses if response.status_code == 201
        )
        winner_id = int(_json_body(success)["id"])

        async with async_engine.connect() as conn:
            active_pairs = await _active_seat_pairs(conn, showtime_id)
            reservation_ids = await _reservation_ids_for_users(conn, user_ids)

        assert active_pairs == [(winner_id, seat_id)], active_pairs
        assert reservation_ids == [winner_id], reservation_ids
    finally:
        await _remove_test_data(
            async_engine,
            user_ids=user_ids,
            showtime_id=showtime_id,
            screen_id=screen_id,
            movie_id=movie_id,
        )


async def test_concurrent_overlapping_multi_seat_holds_never_partial_never_double(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """Four overlapping multi-seat holds never partially succeed (FR-2, test 36).

    The four requested sets all overlap pairwise, so at most one request can
    win. Each seat must have at most one active row, the winner must own exactly
    its requested seats, every loser must leave no reservation row, and no
    request may answer 5xx (which would expose a deadlock).
    """
    user_ids: list[int] = []
    movie_id: int | None = None
    screen_id: int | None = None
    showtime_id: int | None = None

    try:
        emails = [f"spec4.concurrent.36.{index}@example.com" for index in range(4)]
        accounts = await _create_users(async_engine, emails)
        user_ids = [user_id for user_id, _headers in accounts]
        movie_id, screen_id, showtime_id, seat_ids = await _create_screening(
            async_engine,
            slug="test-36",
            seats_per_row=6,
        )

        requested_sets = [
            [seat_ids[0], seat_ids[1], seat_ids[2]],
            [seat_ids[2], seat_ids[3], seat_ids[4]],
            [seat_ids[4], seat_ids[5], seat_ids[0]],
            [seat_ids[1], seat_ids[3], seat_ids[5]],
        ]
        responses = await _gather_after_barrier(
            [
                partial(
                    _hold,
                    committing_client,
                    headers,
                    showtime_id,
                    requested,
                )
                for (_user_id, headers), requested in zip(
                    accounts,
                    requested_sets,
                    strict=True,
                )
            ]
        )

        statuses = [response.status_code for response in responses]
        assert all(status in (201, 409) for status in statuses), [
            (response.status_code, response.text) for response in responses
        ]
        assert statuses.count(201) == 1, [
            (response.status_code, response.text) for response in responses
        ]
        for response in responses:
            if response.status_code == 409:
                assert _error_code(response) == _SEAT_UNAVAILABLE, response.text

        winner_index = statuses.index(201)
        winner_id = int(_json_body(responses[winner_index])["id"])
        winner_seats = sorted(requested_sets[winner_index])
        loser_user_ids = [
            user_ids[index]
            for index, response in enumerate(responses)
            if response.status_code != 201
        ]

        async with async_engine.connect() as conn:
            active_pairs = await _active_seat_pairs(conn, showtime_id)
            stored_winner_seats = await _reservation_seat_ids(conn, winner_id)
            loser_reservations = await _reservation_ids_for_users(
                conn,
                loser_user_ids,
            )

        active_seats = [seat_id for _reservation_id, seat_id in active_pairs]
        assert active_pairs == [(winner_id, seat_id) for seat_id in winner_seats], (
            active_pairs
        )
        assert len(active_seats) == len(set(active_seats)), active_pairs
        assert stored_winner_seats == winner_seats, stored_winner_seats
        assert loser_reservations == [], loser_reservations
    finally:
        await _remove_test_data(
            async_engine,
            user_ids=user_ids,
            showtime_id=showtime_id,
            screen_id=screen_id,
            movie_id=movie_id,
        )


async def test_concurrent_random_holds_keep_all_invariants(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """Thirty random 1-3 seat holds preserve every invariant (FR-2, test 37).

    The fixed ``random.Random(1234)`` seed makes the requested sets reproducible.
    The test checks that each seat has at most one active row, that successes
    plus failures equal thirty, that every success owns exactly its requested
    seats, that there are as many reservation rows as successes, and that no
    reservation exists without seat rows.
    """
    user_ids: list[int] = []
    movie_id: int | None = None
    screen_id: int | None = None
    showtime_id: int | None = None

    try:
        emails = [f"spec4.concurrent.37.{index}@example.com" for index in range(30)]
        accounts = await _create_users(async_engine, emails)
        user_ids = [user_id for user_id, _headers in accounts]
        movie_id, screen_id, showtime_id, seat_ids = await _create_screening(
            async_engine,
            slug="test-37",
            seats_per_row=12,
        )

        rng = random.Random(1234)
        requested_sets = [
            rng.sample(seat_ids, rng.randint(1, 3)) for _account in accounts
        ]
        responses = await _gather_after_barrier(
            [
                partial(
                    _hold,
                    committing_client,
                    headers,
                    showtime_id,
                    requested,
                )
                for (_user_id, headers), requested in zip(
                    accounts,
                    requested_sets,
                    strict=True,
                )
            ]
        )

        statuses = [response.status_code for response in responses]
        assert all(status in (201, 409) for status in statuses), [
            (response.status_code, response.text) for response in responses
        ]
        success_indices = [
            index for index, status in enumerate(statuses) if status == 201
        ]
        failure_indices = [
            index for index, status in enumerate(statuses) if status == 409
        ]
        assert len(success_indices) + len(failure_indices) == 30, statuses
        for index in failure_indices:
            assert _error_code(responses[index]) == _SEAT_UNAVAILABLE, responses[
                index
            ].text

        success_ids = {
            index: int(_json_body(responses[index])["id"]) for index in success_indices
        }

        async with async_engine.connect() as conn:
            active_pairs = await _active_seat_pairs(conn, showtime_id)
            reservation_ids = await _reservation_ids_for_users(conn, user_ids)
            stored_seats = {
                index: await _reservation_seat_ids(conn, reservation_id)
                for index, reservation_id in success_ids.items()
            }

        active_seat_counts = Counter(seat_id for _rid, seat_id in active_pairs)
        assert all(count <= 1 for count in active_seat_counts.values()), (
            active_seat_counts
        )
        assert set(reservation_ids) == set(success_ids.values()), reservation_ids
        assert len(active_pairs) == sum(
            len(requested_sets[index]) for index in success_indices
        ), active_pairs
        for index in success_indices:
            assert stored_seats[index] == sorted(requested_sets[index]), (
                index,
                stored_seats[index],
                requested_sets[index],
            )
    finally:
        await _remove_test_data(
            async_engine,
            user_ids=user_ids,
            showtime_id=showtime_id,
            screen_id=screen_id,
            movie_id=movie_id,
        )


async def test_concurrent_confirm_of_stale_hold_and_new_hold_on_same_seat(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """A stale confirm and competing hold resolve without double-booking (test 38).

    A held reservation older than ``HOLD_MINUTES`` is inserted by SQL. Its owner
    confirms it while another user holds the same seat. The confirm must get 409
    ``RESERVATION_EXPIRED``, the hold must get 201, and exactly one active seat
    row may remain.
    """
    user_ids: list[int] = []
    movie_id: int | None = None
    screen_id: int | None = None
    showtime_id: int | None = None

    try:
        emails = [f"spec4.concurrent.38.{index}@example.com" for index in range(2)]
        accounts = await _create_users(async_engine, emails)
        owner_id, owner_headers = accounts[0]
        other_id, other_headers = accounts[1]
        user_ids = [owner_id, other_id]
        movie_id, screen_id, showtime_id, seat_ids = await _create_screening(
            async_engine,
            slug="test-38",
            seats_per_row=1,
        )
        seat_id = seat_ids[0]

        stale_created_at = datetime.now(UTC) - timedelta(
            minutes=get_settings().hold_minutes + 1
        )
        async with async_engine.begin() as conn:
            stale_id = int(
                (
                    await conn.execute(
                        insert(reservations)
                        .values(
                            user_id=owner_id,
                            showtime_id=showtime_id,
                            status=DEFAULT_RESERVATION_STATUS,
                            created_at=stale_created_at,
                        )
                        .returning(reservations.c.id)
                    )
                ).scalar_one()
            )
            await conn.execute(
                insert(seat_reservations).values(
                    reservation_id=stale_id,
                    showtime_id=showtime_id,
                    seat_id=seat_id,
                    status=DEFAULT_RESERVATION_STATUS,
                )
            )

        blocker_connection = await async_engine.connect()
        blocker_transaction = await blocker_connection.begin()
        blocker_released = False
        try:
            # Hold the second user's row lock so the hold request parks before
            # its lazy-expiry step. Both HTTP requests are still released
            # together; this only fixes which transaction reaches the stale
            # reservation first, exercising the required RESERVATION_EXPIRED
            # path rather than the equally valid stored-expired path.
            await blocker_connection.execute(
                select(users.c.id).where(users.c.id == other_id).with_for_update()
            )
            confirm_task, hold_task = await _start_after_barrier(
                [
                    lambda: _confirm(committing_client, owner_headers, stale_id),
                    lambda: _hold(
                        committing_client,
                        other_headers,
                        showtime_id,
                        [seat_id],
                    ),
                ]
            )

            async def _confirm_then_release() -> httpx.Response:
                """Await the confirm, then let the blocked hold proceed."""
                nonlocal blocker_released
                try:
                    return await confirm_task
                finally:
                    await blocker_transaction.rollback()
                    blocker_released = True

            confirm_response, hold_response = await asyncio.gather(
                _confirm_then_release(),
                hold_task,
            )
        finally:
            if not blocker_released:
                await blocker_transaction.rollback()
            await blocker_connection.close()

        assert all(
            response.status_code < 500 for response in (confirm_response, hold_response)
        ), [
            (confirm_response.status_code, confirm_response.text),
            (hold_response.status_code, hold_response.text),
        ]
        assert confirm_response.status_code == 409, confirm_response.text
        assert _error_code(confirm_response) == _RESERVATION_EXPIRED, (
            confirm_response.text
        )
        assert hold_response.status_code == 201, hold_response.text
        new_reservation_id = int(_json_body(hold_response)["id"])

        async with async_engine.connect() as conn:
            active_pairs = await _active_seat_pairs(conn, showtime_id)
            stale_status = await _stored_status(conn, stale_id)
            stale_seat_statuses = await _stored_seat_statuses(conn, stale_id)
            new_status = await _stored_status(conn, new_reservation_id)
            new_seat_statuses = await _stored_seat_statuses(
                conn,
                new_reservation_id,
            )

        assert active_pairs == [(new_reservation_id, seat_id)], active_pairs
        assert stale_status == "expired", stale_status
        assert stale_seat_statuses == ["expired"], stale_seat_statuses
        assert new_status == "held", new_status
        assert new_seat_statuses == ["held"], new_seat_statuses
    finally:
        await _remove_test_data(
            async_engine,
            user_ids=user_ids,
            showtime_id=showtime_id,
            screen_id=screen_id,
            movie_id=movie_id,
        )


async def test_concurrent_double_hold_by_same_user_exactly_one_succeeds(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """Two simultaneous holds by one user: exactly one wins (FR-6, test 39).

    The two requests ask for different seats on the same showtime. The user row
    lock serialises them, so one gets 201 and the other gets 409
    ``ACTIVE_HOLD_EXISTS``; exactly one reservation and one active seat row are
    stored.
    """
    user_ids: list[int] = []
    movie_id: int | None = None
    screen_id: int | None = None
    showtime_id: int | None = None

    try:
        accounts = await _create_users(
            async_engine,
            ["spec4.concurrent.39@example.com"],
        )
        user_id, headers = accounts[0]
        user_ids = [user_id]
        movie_id, screen_id, showtime_id, seat_ids = await _create_screening(
            async_engine,
            slug="test-39",
            seats_per_row=2,
        )

        responses = await _gather_after_barrier(
            [
                lambda: _hold(
                    committing_client,
                    headers,
                    showtime_id,
                    [seat_ids[0]],
                ),
                lambda: _hold(
                    committing_client,
                    headers,
                    showtime_id,
                    [seat_ids[1]],
                ),
            ]
        )

        statuses = sorted(response.status_code for response in responses)
        assert statuses == [201, 409], [
            (response.status_code, response.text) for response in responses
        ]
        conflict = next(
            response for response in responses if response.status_code == 409
        )
        assert _error_code(conflict) == _ACTIVE_HOLD_EXISTS, conflict.text

        success = next(
            response for response in responses if response.status_code == 201
        )
        winner_id = int(_json_body(success)["id"])
        winner_seat_ids = [
            int(seat["seat_id"]) for seat in _json_body(success)["seats"]
        ]

        async with async_engine.connect() as conn:
            active_pairs = await _active_seat_pairs(conn, showtime_id)
            reservation_ids = await _reservation_ids_for_users(conn, user_ids)
            stored_seats = await _reservation_seat_ids(conn, winner_id)

        assert reservation_ids == [winner_id], reservation_ids
        assert active_pairs == [(winner_id, winner_seat_ids[0])], active_pairs
        assert stored_seats == sorted(winner_seat_ids), stored_seats
    finally:
        await _remove_test_data(
            async_engine,
            user_ids=user_ids,
            showtime_id=showtime_id,
            screen_id=screen_id,
            movie_id=movie_id,
        )


async def test_database_still_rejects_duplicate_active_row_when_service_is_bypassed(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """The partial unique index rejects a duplicate active row (test 40).

    Two raw SQL transactions insert the same ``(showtime_id, seat_id)`` pair as
    held rows, without going through the reservation service. The first commits,
    the second must raise ``IntegrityError`` with SQLSTATE ``23505``, and exactly
    one active row may remain. This proves the database guarantee independently
    of the application.
    """
    user_ids: list[int] = []
    movie_id: int | None = None
    screen_id: int | None = None
    showtime_id: int | None = None

    try:
        accounts = await _create_users(
            async_engine,
            [
                "spec4.concurrent.40.first@example.com",
                "spec4.concurrent.40.second@example.com",
            ],
        )
        user_ids = [user_id for user_id, _headers in accounts]
        movie_id, screen_id, showtime_id, seat_ids = await _create_screening(
            async_engine,
            slug="test-40",
            seats_per_row=1,
        )
        seat_id = seat_ids[0]

        async with async_engine.begin() as conn:
            first_reservation_id = int(
                (
                    await conn.execute(
                        insert(reservations)
                        .values(
                            user_id=user_ids[0],
                            showtime_id=showtime_id,
                            status=DEFAULT_RESERVATION_STATUS,
                            created_at=datetime.now(UTC),
                        )
                        .returning(reservations.c.id)
                    )
                ).scalar_one()
            )
            await conn.execute(
                insert(seat_reservations).values(
                    reservation_id=first_reservation_id,
                    showtime_id=showtime_id,
                    seat_id=seat_id,
                    status=DEFAULT_RESERVATION_STATUS,
                )
            )

        second_error: IntegrityError | None = None
        try:
            async with async_engine.begin() as conn:
                second_reservation_id = int(
                    (
                        await conn.execute(
                            insert(reservations)
                            .values(
                                user_id=user_ids[1],
                                showtime_id=showtime_id,
                                status=DEFAULT_RESERVATION_STATUS,
                                created_at=datetime.now(UTC),
                            )
                            .returning(reservations.c.id)
                        )
                    ).scalar_one()
                )
                await conn.execute(
                    insert(seat_reservations).values(
                        reservation_id=second_reservation_id,
                        showtime_id=showtime_id,
                        seat_id=seat_id,
                        status=DEFAULT_RESERVATION_STATUS,
                    )
                )
        except IntegrityError as error:
            second_error = error

        assert second_error is not None, "duplicate active seat row was accepted"
        sqlstate = getattr(second_error.orig, "sqlstate", None)
        assert sqlstate == "23505", (sqlstate, str(second_error))

        async with async_engine.connect() as conn:
            active_pairs = await _active_seat_pairs(conn, showtime_id)
            reservation_ids = await _reservation_ids_for_users(conn, user_ids)

        assert active_pairs == [(first_reservation_id, seat_id)], active_pairs
        assert reservation_ids == [first_reservation_id], reservation_ids
    finally:
        await _remove_test_data(
            async_engine,
            user_ids=user_ids,
            showtime_id=showtime_id,
            screen_id=screen_id,
            movie_id=movie_id,
        )
