"""Concurrency integration test for the SPEC-6 reports (test 29).

The scenario is SPEC-6 Section 5, test 29: twenty different signed-in users each
hold one seat of one showtime and confirm it, while twenty report reads (revenue
and capacity, alternating) run at the same time. All forty tasks are released
together through an ``asyncio.Event``, so the reads overlap the confirmations in
the event loop and in PostgreSQL instead of being serialised by test setup.

What the test proves (FR-5, FR-7, FR-8 under concurrency):

* every revenue response is internally consistent -- ``total_cents`` equals the
  sum of its own rows and equals ``tickets_sold * price_cents``, because the
  report is one SQL statement over one snapshot and one ticket costs one
  showtime's price;
* every capacity response is internally consistent -- ``sold_seats + held_seats
  <= total_seats`` and ``available_seats == total - sold - held``, which is the
  invariant the partial unique index on ``seat_reservations`` guarantees;
* no read answers 5xx, however the interleaving falls;
* after every task finished, revenue is exactly ``confirmed seats * price`` and
  capacity reports the same number of sold seats -- revenue equals
  ``sold_seats * price_cents`` (FR-7), which also shows the reports wrote
  nothing (FR-8): the only writer in this test is the API's own confirm route.

It runs on the committed ``committing_client`` fixture, so each request gets its
own connection and transaction, exactly as in production. It therefore creates
its own administrator, twenty users, a movie, a screen with enough seats and one
showtime with a known price, and deletes every row it created in a ``finally``
block, so a failed assertion cannot leave committed rows behind.

The reports are global, so both reads are filtered -- revenue by ``movie_id`` and
capacity by ``showtime_id`` -- to isolate this test's showtime from any committed
row another test may have left behind.

The helpers below are local copies of the ones the SPEC-4 and SPEC-5 concurrency
modules keep privately (they are not in ``tests/conftest.py``), and are
deliberately the smallest set this test needs: build committed test data, send
the two reservation calls, and clean up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import httpx
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.config import get_settings
from src.core.enums import ADMIN_USER_ROLE, DEFAULT_USER_ROLE, UserRole
from src.core.security import create_access_token, hash_password
from src.db.tables import movies, reservations, screens, seats, showtimes, users

_REVENUE_URL = "/admin/reports/revenue"
_CAPACITY_URL = "/admin/reports/capacity"
_RESERVATIONS_URL = "/reservations"

# One price for the whole fixture (SPEC-6 test 29), chosen so that a revenue
# total which mixed up seats with cents -- or used another showtime's price --
# cannot coincide with the expected value.
_PRICE_CENTS = 1_234

# Seats on the screen: more than the number of buyers, so the capacity report
# really is a subtraction rather than a saturated total.
_TOTAL_SEATS = 25

# Buyers, and the number of report reads racing them (SPEC-6 test 29).
_BUYERS = 20
_READS = 20

# Labels identifying each task's result, so the gathered results can be checked
# according to the kind of request that produced them.
_LABEL_WRITE = "write"
_LABEL_REVENUE = "revenue"
_LABEL_CAPACITY = "capacity"

# Ordinary throwaway test data, never a credential (IMPLEMENTER RULES, rule 6).
_USER_PASSWORD = "spec-6 concurrency test password"

# One task's outcome: the label above and the responses it produced (two for a
# hold-then-confirm, one for a report read).
_TaskResult = tuple[str, list[httpx.Response]]
_TaskFactory = Callable[[], Awaitable[_TaskResult]]

# ``(user id, Authorization header)`` as returned by :func:`_create_accounts`.
_Account = tuple[int, dict[str, str]]


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


async def _create_accounts(
    engine: AsyncEngine,
    emails: Sequence[str],
    *,
    role: UserRole,
) -> list[_Account]:
    """Insert committed user rows and mint one bearer header per account.

    The accounts are created directly rather than through signup because this
    test exercises the report transactions; bcrypt work for twenty-one accounts
    would only slow the gate. The token is minted with the same
    :func:`src.core.security.create_access_token` the login route uses, so the
    application authenticates each account through its normal dependency.

    Args:
        engine: The committing test engine.
        emails: Unique, already-normalized email addresses.
        role: The stored role, ``admin`` for the report reader or ``user`` for
            the buyers.

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
                            "role": role,
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
    price_cents: int,
) -> tuple[int, int, int, list[int]]:
    """Insert a committed movie, screen, seats and one future showtime.

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
                    .values(title=f"SPEC-6 concurrency {slug}")
                    .returning(movies.c.id)
                )
            ).scalar_one()
        )
        screen_id = int(
            (
                await conn.execute(
                    insert(screens)
                    .values(
                        name=f"SPEC-6 concurrency screen {slug}",
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
    """Delete every committed row this test created.

    Deletion order follows the foreign keys: reservations first (their
    ``seat_reservations`` rows cascade), then the showtime, the seats, the
    screen, the movie and finally the users. Every id is optional and
    reservations are deleted both by owner and by showtime, so cleanup still
    succeeds when setup failed part-way through or a write task failed before
    confirming.

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


async def _hold(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    showtime_id: int,
    seat_id: int,
) -> httpx.Response:
    """Post a one-seat hold and return its raw response.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting buyer's ``Authorization`` header.
        showtime_id: The showtime to hold a seat on.
        seat_id: The requested seat id.

    Returns:
        httpx.Response: The unasserted response; the caller checks the status.
    """
    return await client.post(
        _RESERVATIONS_URL,
        json={"showtime_id": showtime_id, "seat_ids": [seat_id]},
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
        headers: The owning user's ``Authorization`` header.
        reservation_id: The reservation to confirm.

    Returns:
        httpx.Response: The unasserted response; the caller checks the status.
    """
    return await client.post(
        f"{_RESERVATIONS_URL}/{reservation_id}/confirm",
        headers=headers,
    )


async def _hold_and_confirm(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    showtime_id: int,
    seat_id: int,
) -> _TaskResult:
    """Hold one seat and confirm it in the same task, as one buyer would.

    The two requests are sequential on purpose: they belong to one user, who may
    hold only one active reservation per showtime at a time. When the hold is
    refused the confirmation is skipped, so the result carries a single response
    and the caller reports that failure instead of masking it with a second one.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting buyer's ``Authorization`` header.
        showtime_id: The showtime to buy a seat on.
        seat_id: The seat this buyer wants.

    Returns:
        _TaskResult: ``("write", [hold])`` when the hold failed, otherwise
        ``("write", [hold, confirm])``.
    """
    hold_response = await _hold(client, headers, showtime_id, seat_id)
    if hold_response.status_code != 201:
        return (_LABEL_WRITE, [hold_response])

    reservation_id = int(_json_body(hold_response)["id"])
    confirm_response = await _confirm(client, headers, reservation_id)
    return (_LABEL_WRITE, [hold_response, confirm_response])


async def _read_revenue(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    movie_id: int,
) -> _TaskResult:
    """Read the revenue report for this test's movie.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The administrator's ``Authorization`` header.
        movie_id: The test's movie id, used as the isolating report filter.

    Returns:
        _TaskResult: ``("revenue", [response])``.
    """
    response = await client.get(
        _REVENUE_URL,
        params={"movie_id": movie_id},
        headers=headers,
    )
    return (_LABEL_REVENUE, [response])


async def _read_capacity(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    showtime_id: int,
) -> _TaskResult:
    """Read the capacity report for this test's showtime.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The administrator's ``Authorization`` header.
        showtime_id: The test's showtime id, used as the isolating filter.

    Returns:
        _TaskResult: ``("capacity", [response])``.
    """
    response = await client.get(
        _CAPACITY_URL,
        params={"showtime_id": showtime_id},
        headers=headers,
    )
    return (_LABEL_CAPACITY, [response])


async def _gather_after_barrier(
    requests: Sequence[_TaskFactory],
) -> list[_TaskResult]:
    """Run every task together, released at the same instant.

    Each task increments a ready counter under a lock and then waits on one
    ``asyncio.Event``; the task that completes the count sets the event. The
    requests therefore start as one group instead of one after another, which is
    what makes the confirmations overlap the report reads.

    Args:
        requests: Zero-argument callables that each run one task. They must be
            non-empty.

    Returns:
        list[_TaskResult]: The results in the same order as ``requests``
        (``asyncio.gather`` preserves argument order), so the caller can match
        every result to the request kind it created.
    """
    assert requests, "at least one request is required"
    start = asyncio.Event()
    ready_count = 0
    ready_lock = asyncio.Lock()

    async def _run(request: _TaskFactory) -> _TaskResult:
        """Mark readiness, wait for the group release, then run the task."""
        nonlocal ready_count
        async with ready_lock:
            ready_count += 1
            if ready_count == len(requests):
                start.set()
        await start.wait()
        return await request()

    tasks = [asyncio.create_task(_run(request)) for request in requests]
    await start.wait()
    return list(await asyncio.gather(*tasks))


def _assert_revenue_invariants(body: dict[str, Any], price_cents: int) -> None:
    """Check the FR-5 and FR-7 invariants of one revenue response.

    A revenue report is built from a single SQL statement, so its totals and its
    rows always agree; and because every ticket of this fixture costs the same
    ``price_cents``, its money is exactly ``tickets_sold * price_cents``. The
    values must be ``int`` -- money is never a float.

    Args:
        body: The parsed body of ``GET /admin/reports/revenue``.
        price_cents: The single price of this test's showtime.

    Returns:
        None: It raises on a violated invariant.

    Raises:
        AssertionError: If the money is not an integer, the totals are not the
            sums of the rows, or the revenue is not ``tickets_sold * price``.
    """
    rows = body["rows"]
    assert isinstance(rows, list), body
    total_cents = body["total_cents"]
    tickets_sold = body["tickets_sold"]
    assert type(total_cents) is int, body
    assert type(tickets_sold) is int, body
    rows_revenue = sum(int(row["revenue_cents"]) for row in rows)
    rows_tickets = sum(int(row["tickets_sold"]) for row in rows)
    assert total_cents == rows_revenue, body
    assert tickets_sold == rows_tickets, body
    assert total_cents == int(tickets_sold) * price_cents, body


def _assert_capacity_invariants(body: dict[str, Any], total_seats: int) -> None:
    """Check the capacity invariants of one capacity response.

    The partial unique index on ``seat_reservations`` allows at most one active
    row per seat, so no response may ever claim more sold plus held seats than
    the screen has, and ``available_seats`` must be exactly the subtraction.

    Args:
        body: The parsed body of ``GET /admin/reports/capacity``, filtered to
            this test's single showtime.
        total_seats: The number of seats on the test's screen.

    Returns:
        None: It raises on a violated invariant.

    Raises:
        AssertionError: If the page does not hold exactly this test's one
            showtime, the numbers are not integers, ``sold + held`` exceeds the
            screen's seats, or ``available_seats`` is not the subtraction.
    """
    items = body["items"]
    assert isinstance(items, list), body
    assert len(items) == 1, body
    item = items[0]
    assert isinstance(item, dict), body
    total = item["total_seats"]
    sold = item["sold_seats"]
    held = item["held_seats"]
    available = item["available_seats"]
    for name, value in (
        ("total_seats", total),
        ("sold_seats", sold),
        ("held_seats", held),
        ("available_seats", available),
    ):
        assert type(value) is int, (name, item)
    assert total == total_seats, item
    assert sold + held <= total, item
    assert available == total - sold - held, item
    assert 0 <= sold <= _BUYERS, item
    assert 0 <= held <= _BUYERS, item


async def test_revenue_stays_consistent_while_confirmations_happen(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """Prove revenue and capacity stay consistent while confirmations commit.

    Twenty buyers each hold one seat of one showtime and confirm it, while twenty
    report reads -- revenue and capacity alternating -- run at the same time; all
    forty tasks are released together with an ``asyncio.Event``. Every write must
    succeed (each buyer owns a different seat), every read must answer 200 and
    satisfy its invariants, and once everything has finished revenue must be
    exactly ``confirmed seats * price`` with capacity reporting the same number
    of sold seats (SPEC-6 test 29; FR-5, FR-7, FR-8).

    Args:
        committing_client: A client whose every request commits its own
            transaction, so the reads race the writes instead of sharing one
            snapshot.
        async_engine: The per-test engine, used for setup and for cleanup.

    Returns:
        None: It raises on any violated invariant.
    """
    admin_ids: list[int] = []
    buyer_ids: list[int] = []
    movie_id: int | None = None
    screen_id: int | None = None
    showtime_id: int | None = None

    try:
        admin_id, admin_headers = (
            await _create_accounts(
                async_engine,
                ["spec6.concurrent.29.admin@example.com"],
                role=ADMIN_USER_ROLE,
            )
        )[0]
        admin_ids.append(admin_id)

        buyer_accounts = await _create_accounts(
            async_engine,
            [
                f"spec6.concurrent.29.user{index:02d}@example.com"
                for index in range(1, _BUYERS + 1)
            ],
            role=DEFAULT_USER_ROLE,
        )
        buyer_ids = [buyer_id for buyer_id, _headers in buyer_accounts]

        movie_id, screen_id, showtime_id, seat_ids = await _create_screening(
            async_engine,
            slug="test-29",
            seats_per_row=_TOTAL_SEATS,
            price_cents=_PRICE_CENTS,
        )
        assert len(seat_ids) >= _BUYERS, seat_ids

        requests: list[_TaskFactory] = [
            partial(
                _hold_and_confirm,
                committing_client,
                buyer_headers,
                showtime_id,
                seat_ids[index],
            )
            for index, (_buyer_id, buyer_headers) in enumerate(buyer_accounts)
        ]
        for index in range(_READS):
            if index % 2 == 0:
                requests.append(
                    partial(
                        _read_revenue,
                        committing_client,
                        admin_headers,
                        movie_id,
                    )
                )
            else:
                requests.append(
                    partial(
                        _read_capacity,
                        committing_client,
                        admin_headers,
                        showtime_id,
                    )
                )

        results = await _gather_after_barrier(requests)

        writes = 0
        revenue_reads = 0
        capacity_reads = 0
        for label, responses in results:
            for response in responses:
                assert response.status_code < 500, (
                    label,
                    response.status_code,
                    response.text,
                )
            if label == _LABEL_WRITE:
                writes += 1
                assert len(responses) == 2, (
                    responses[0].status_code,
                    responses[0].text,
                )
                hold_response, confirm_response = responses
                assert hold_response.status_code == 201, hold_response.text
                assert confirm_response.status_code == 200, confirm_response.text
            elif label == _LABEL_REVENUE:
                revenue_reads += 1
                assert responses[0].status_code == 200, responses[0].text
                _assert_revenue_invariants(_json_body(responses[0]), _PRICE_CENTS)
            else:
                capacity_reads += 1
                assert responses[0].status_code == 200, responses[0].text
                _assert_capacity_invariants(_json_body(responses[0]), _TOTAL_SEATS)

        assert writes == _BUYERS, writes
        assert revenue_reads + capacity_reads == _READS, (revenue_reads, capacity_reads)

        revenue_response = await committing_client.get(
            _REVENUE_URL,
            params={"movie_id": movie_id},
            headers=admin_headers,
        )
        assert revenue_response.status_code == 200, revenue_response.text
        final_revenue = _json_body(revenue_response)
        _assert_revenue_invariants(final_revenue, _PRICE_CENTS)
        assert final_revenue["tickets_sold"] == _BUYERS, final_revenue
        assert final_revenue["total_cents"] == _BUYERS * _PRICE_CENTS, final_revenue
        final_rows = final_revenue["rows"]
        assert isinstance(final_rows, list) and len(final_rows) == 1, final_revenue
        assert final_rows[0]["movie_id"] == movie_id, final_rows

        capacity_response = await committing_client.get(
            _CAPACITY_URL,
            params={"showtime_id": showtime_id},
            headers=admin_headers,
        )
        assert capacity_response.status_code == 200, capacity_response.text
        final_capacity = _json_body(capacity_response)
        _assert_capacity_invariants(final_capacity, _TOTAL_SEATS)
        final_item = final_capacity["items"][0]
        assert final_item["showtime"]["id"] == showtime_id, final_item
        assert final_item["sold_seats"] == _BUYERS, final_item
        assert final_item["held_seats"] == 0, final_item
        assert final_item["available_seats"] == _TOTAL_SEATS - _BUYERS, final_item
    finally:
        await _remove_test_data(
            async_engine,
            user_ids=[*admin_ids, *buyer_ids],
            showtime_id=showtime_id,
            screen_id=screen_id,
            movie_id=movie_id,
        )
