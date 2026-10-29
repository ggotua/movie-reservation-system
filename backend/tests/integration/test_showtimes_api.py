"""Integration tests for the screen and showtime endpoints (SPEC-3 tests 25-42).

The harness is the same as ``test_movies_api.py``: the real application is
driven through an httpx ``ASGITransport`` client against a real PostgreSQL
server (``TEST_DATABASE_URL``), and every request of one test shares ONE
connection inside an outer transaction that is rolled back at teardown -- so
nothing here survives the test that wrote it, with the single exception of test
33, which needs two independent transactions and therefore cleans up its rows
in ``finally`` (see ``tests/conftest.py``). The setup these tests share -- an
acting administrator, a movie, a screen and a showtime -- comes from the SPEC-3
fixtures in ``conftest.py``.

What each test pins down, by requirement:

* 25-26 FR-9  seat generation: exactly ``rows * seats_per_row`` seats, row
        labels ``A``.. and numbers ``1``.. (up to ``Z`` / 40), all ``standard``;
* 27    FR-9  a duplicate screen name is refused and leaves no orphan seat;
* 28    FR-9  ``GET /admin/screens`` lists every screen, ordered by name;
* 29-30 FR-10, FR-15 showtime create: 201, a UTC response, and an offset input
        normalised to the very same instant;
* 31    FR-10 the "strictly in the future" boundary, checked at service level;
* 32    FR-5  an unknown movie or screen is a 404 and creates nothing;
* 33    FR-11 two simultaneous overlapping creates: exactly one 201;
* 34    FR-11 the overlap window: same screen 409, an exact slot gap allowed on
        either side, and another screen never conflicts;
* 35    FR-10 the ``price_cents`` boundaries;
* 36-38 FR-12 delete: 204 for a future showtime, 409 once it has started, 409
        with a reservation (inserted directly with SQL);
* 39-42 FR-13, FR-14 browse: the local-day window, ordering, the embedded movie
        and screen, the ``movie_id`` filter, the page envelope, and the rejected
        ``date`` / ``limit`` / ``offset`` values.

Every date and instant is built relative to ``now`` rather than hard-coded, so
the suite keeps working on whatever day it runs, and every failure case asserts
the status, the error code and that nothing was written.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
import pytest
from sqlalchemy import ColumnElement, Table, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.core.config import get_settings
from src.core.enums import ADMIN_USER_ROLE
from src.core.errors import ApiError
from src.db.tables import movies, reservations, screens, seats, showtimes, users
from src.showtimes import service
from src.showtimes.schemas import ShowtimeCreate
from tests.conftest import _cinema_zone, _local_day, _local_instant

# The endpoints under test (SPEC-3 Section 2.3).
_SHOWTIMES_URL = "/showtimes"
_ADMIN_SCREENS_URL = "/admin/screens"
_ADMIN_SHOWTIMES_URL = "/admin/showtimes"
_ADMIN_MOVIES_URL = "/admin/movies"
_SIGNUP_URL = "/auth/signup"
_LOGIN_URL = "/auth/login"

# The id used wherever a row must not exist. Far above anything this suite could
# realistically generate, so a 404 here can never be an existing row.
_UNKNOWN_ID = 999_999_999

# A throwaway password for the accounts these tests sign up. Ordinary test data,
# never a credential (IMPLEMENTER RULES, rule 6).
_USER_PASSWORD = "spec3 user password"

# The actor id passed to the service when a test calls it directly instead of
# going through a route. The service uses it for the audit log line only -- it is
# never written to a row -- so any positive integer is equally valid.
_ACTOR_ID = 1


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


def _json_list(response: httpx.Response) -> list[dict[str, Any]]:
    """Return a response body as a JSON list of objects.

    Used where an endpoint answers with a bare list rather than a page
    envelope: ``GET /admin/screens``.

    Args:
        response: The response to read.

    Returns:
        list[dict[str, Any]]: The parsed body.

    Raises:
        AssertionError: If the body is not a list of JSON objects, with it
            attached.
    """
    body: Any = response.json()
    assert isinstance(body, list), body
    assert all(isinstance(item, dict) for item in body), body
    return body


def _error_payload(response: httpx.Response) -> dict[str, str]:
    """Return the ``error`` object of a failure response, asserting its shape.

    The exact body ``{"error": {"code": ..., "message": ...}}`` and nothing else
    is what SPEC-3 FR-16 requires of every failure from these endpoints.

    Args:
        response: The failure response.

    Returns:
        dict[str, str]: The ``code`` and ``message`` of the failure.

    Raises:
        AssertionError: If the body does not have the standard shape.
    """
    body = _json_body(response)
    assert set(body) == {"error"}, body
    error = body["error"]
    assert isinstance(error, dict), body
    assert set(error) == {"code", "message"}, body
    assert isinstance(error["code"], str) and error["code"], body
    assert isinstance(error["message"], str) and error["message"], body
    return error


def _assert_error(response: httpx.Response, status: int, code: str) -> None:
    """Assert a failure response carries ``status`` and the expected code.

    Args:
        response: The failure response.
        status: The expected HTTP status.
        code: The expected machine-readable error code.
    """
    assert response.status_code == status, response.text
    assert _error_payload(response)["code"] == code, response.text


def _parse_instant(value: str) -> datetime:
    """Parse an ISO 8601 timestamp from a response body.

    Args:
        value: The timestamp, e.g. ``"2026-10-11T20:00:00Z"``.

    Returns:
        datetime: The parsed, timezone-aware instant.
    """
    return datetime.fromisoformat(value)


def _assert_utc_instant(value: str, expected: datetime) -> None:
    """Assert a serialised instant is the UTC form of ``expected``.

    The instant must denote the same moment (FR-15) and it must be expressed in
    UTC: the string either ends in the ``Z`` designator or carries a ``+00:00``
    offset, so a response can never silently expose the server's local zone.

    Args:
        value: The ``starts_at`` string from a response body.
        expected: The timezone-aware instant the field must denote.
    """
    assert value.endswith(("Z", "+00:00")), value
    parsed = _parse_instant(value)
    assert parsed == expected, value
    assert parsed.utcoffset() == timedelta(0), value


def _showtime_payload(
    movie_id: int,
    screen_id: int,
    starts_at: datetime,
    price_cents: int,
) -> dict[str, Any]:
    """Build a ``POST /admin/showtimes`` body (SPEC-3 Section 2.3, FR-10).

    A raw body rather than the ``create_showtime_via_api`` fixture, because most
    of these tests assert on a failure: the fixture would raise on the very
    status they expect. ``starts_at`` is sent with ``isoformat()``, so a UTC
    instant is submitted as ``+00:00`` and a value built in another zone keeps
    its own offset -- the API normalises both to the same instant (FR-15).

    Args:
        movie_id: The id of an existing movie.
        screen_id: The id of an existing screen.
        starts_at: A timezone-aware start instant.
        price_cents: Ticket price in integer cents, 1..100 000 000.

    Returns:
        dict[str, Any]: The request body.
    """
    return {
        "movie_id": movie_id,
        "screen_id": screen_id,
        "starts_at": starts_at.isoformat(),
        "price_cents": price_cents,
    }


async def _post_showtime(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> httpx.Response:
    """Post a showtime body and return the raw response.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting administrator's ``Authorization`` header.
        payload: The request body, e.g. from :func:`_showtime_payload`.

    Returns:
        httpx.Response: The response, unasserted, so a test can check any
        status.
    """
    return await client.post(_ADMIN_SHOWTIMES_URL, json=payload, headers=headers)


async def _count_where(
    conn: AsyncConnection,
    table: Table,
    *conditions: ColumnElement[bool],
) -> int:
    """Count the rows of a table matching every condition.

    Args:
        conn: The test's connection, inside the same transaction as the
            application's requests.
        table: The table to count.
        *conditions: Conditions to apply; none means the whole table.

    Returns:
        int: The number of matching rows.
    """
    return int(
        (
            await conn.execute(
                select(func.count()).select_from(table).where(*conditions)
            )
        ).scalar_one()
    )


async def _seat_rows(
    conn: AsyncConnection,
    screen_id: int,
) -> list[tuple[str, int, str]]:
    """Load a screen's generated seats in a deterministic order.

    Args:
        conn: The test's connection.
        screen_id: The screen whose seats to load.

    Returns:
        list[tuple[str, int, str]]: One ``(row_label, seat_number, seat_type)``
        tuple per seat, ordered by row label and then seat number.
    """
    rows = (
        await conn.execute(
            select(seats.c.row_label, seats.c.seat_number, seats.c.seat_type)
            .where(seats.c.screen_id == screen_id)
            .order_by(seats.c.row_label, seats.c.seat_number)
        )
    ).all()
    return [(row.row_label, row.seat_number, row.seat_type) for row in rows]


async def _insert_screen(conn: AsyncConnection, name: str) -> int:
    """Insert a screen row directly, the way the seed script would.

    Seats are deliberately not generated: SPEC-3 Section 3 states that the seats
    of a screen are never modified, and the tests that call this helper only care
    that the screen is listed.

    Args:
        conn: The test's connection.
        name: The screen name.

    Returns:
        int: The id of the inserted screen.
    """
    return int(
        (
            await conn.execute(
                insert(screens)
                .values(name=name, rows=2, seats_per_row=3)
                .returning(screens.c.id)
            )
        ).scalar_one()
    )


async def _insert_showtime(
    conn: AsyncConnection,
    movie_id: int,
    screen_id: int,
    starts_at: datetime,
    price_cents: int,
) -> int:
    """Insert a showtime row directly, bypassing the scheduling rules.

    Needed by the delete and browse tests: the API refuses a past ``starts_at``
    (FR-10), so a started showtime -- the case ``DELETE`` must answer 409 for --
    can only be set up in the database.

    Args:
        conn: The test's connection.
        movie_id: The id of an existing movie.
        screen_id: The id of an existing screen.
        starts_at: A timezone-aware start instant, past or future.
        price_cents: Ticket price in integer cents.

    Returns:
        int: The id of the inserted showtime.
    """
    return int(
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


# The cinema-zone date builders (``_cinema_zone``, ``_local_day``,
# ``_local_instant``) now live in ``tests/conftest.py`` because the SPEC-6
# reporting tests need the same Tbilisi wall-clock instants; this module imports
# them instead of keeping a second definition.

# The fixture signatures, declared here exactly as ``conftest.py`` declares them
# (a test module cannot import the conftest module itself).
_FutureStart = Callable[[timedelta], datetime]
_AdminHeaders = Callable[
    [httpx.AsyncClient, AsyncConnection, str], Awaitable[dict[str, str]]
]


class _CreateMovie(Protocol):
    """The ``create_movie_via_api`` signature, with ``genre_ids`` optional."""

    async def __call__(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        title: str,
        genre_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Create a movie through ``POST /admin/movies`` (SPEC-3 FR-3)."""
        ...


_CreateScreen = Callable[
    [httpx.AsyncClient, dict[str, str], str, int, int], Awaitable[dict[str, Any]]
]
_CreateShowtime = Callable[
    [httpx.AsyncClient, dict[str, str], int, int, datetime, int],
    Awaitable[dict[str, Any]],
]
_CreateUser = Callable[[httpx.AsyncClient, str, str], Awaitable[httpx.Response]]
_Login = Callable[[httpx.AsyncClient, str, str], Awaitable[str]]


def _assert_page(
    body: dict[str, Any],
    total: int,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Assert the page envelope of SPEC-3 FR-14 and return its items.

    Args:
        body: The parsed response body.
        total: The expected ``total``, i.e. matches ignoring the window.
        limit: The expected echoed ``limit``.
        offset: The expected echoed ``offset``.

    Returns:
        list[dict[str, Any]]: The page's items.
    """
    assert set(body) == {"items", "total", "limit", "offset"}, body
    assert body["total"] == total, body
    assert body["limit"] == limit, body
    assert body["offset"] == offset, body
    items = body["items"]
    assert isinstance(items, list), body
    assert all(isinstance(item, dict) for item in items), body
    return items


def _assert_ordered_by_starts_at(items: Sequence[dict[str, Any]]) -> None:
    """Assert a page is ordered by ``starts_at`` (SPEC-3 Section 2.5).

    Args:
        items: The page's showtime objects.
    """
    instants = [_parse_instant(item["starts_at"]) for item in items]
    assert instants == sorted(instants), items


# --- Screens (tests 25-28) --------------------------------------------------


async def test_create_screen_generates_exactly_rows_times_seats(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
) -> None:
    """A new screen has exactly ``rows * seats_per_row`` seats (FR-9, test 25).

    ``seat_count`` is derived from the dimensions in the response, and the rows
    themselves must exist in the database: one seat per ``(row, number)`` pair,
    with none missing and none duplicated. The name is submitted padded, which
    also proves the API stores it stripped.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.screen.seats@example.com"
    )

    response = await client.post(
        _ADMIN_SCREENS_URL,
        json={"name": "  Spec3 Seat Screen  ", "rows": 3, "seats_per_row": 4},
        headers=headers,
    )

    assert response.status_code == 201, response.text
    body = _json_body(response)
    assert set(body) == {"id", "name", "rows", "seats_per_row", "seat_count"}, body
    assert body["name"] == "Spec3 Seat Screen"
    assert body["rows"] == 3
    assert body["seats_per_row"] == 4
    assert body["seat_count"] == 12

    stored = await _seat_rows(rollback_connection, int(body["id"]))
    assert len(stored) == 12, stored
    assert len(set(stored)) == 12, stored
    assert {label for label, _, _ in stored} == {"A", "B", "C"}, stored


async def test_create_screen_labels_and_types(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_screen_via_api: _CreateScreen,
) -> None:
    """Row labels run ``A``.. and numbers ``1``..; seats are ``standard`` (test 26).

    The label scheme is what keeps a seat addressable, and its two ends are
    pinned by the accepted extremes: 26 rows end at ``Z`` and 40 seats per row
    end at ``40``.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.screen.labels@example.com"
    )

    screen = await create_screen_via_api(client, headers, "Spec3 Label Screen", 3, 2)

    assert await _seat_rows(rollback_connection, int(screen["id"])) == [
        ("A", 1, "standard"),
        ("A", 2, "standard"),
        ("B", 1, "standard"),
        ("B", 2, "standard"),
        ("C", 1, "standard"),
        ("C", 2, "standard"),
    ]

    tall = await create_screen_via_api(client, headers, "Spec3 Tall Screen", 26, 1)
    labels = [
        label for label, _, _ in await _seat_rows(rollback_connection, int(tall["id"]))
    ]
    assert labels == [chr(ord("A") + index) for index in range(26)], labels

    wide = await create_screen_via_api(client, headers, "Spec3 Wide Screen", 1, 40)
    numbers = [
        number
        for _, number, _ in await _seat_rows(rollback_connection, int(wide["id"]))
    ]
    assert numbers == list(range(1, 41)), numbers


async def test_create_screen_duplicate_name_returns_409_and_leaves_no_orphan_seats(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_screen_via_api: _CreateScreen,
) -> None:
    """A duplicate name is refused and writes no seat (FR-9, decision D5, test 27).

    The screen insert runs in a SAVEPOINT and the UNIQUE constraint is the single
    source of truth, so the refusal must leave the surrounding request
    transaction usable and no seat may be created for the rejected screen. The
    name is compared exactly: the database constraint is case-sensitive.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.screen.duplicate@example.com"
    )
    name = "Spec3 Duplicate Screen"
    screen = await create_screen_via_api(client, headers, name, 2, 3)
    seats_before = await _count_where(rollback_connection, seats)

    response = await client.post(
        _ADMIN_SCREENS_URL,
        json={"name": name, "rows": 5, "seats_per_row": 5},
        headers=headers,
    )

    _assert_error(response, 409, "SCREEN_NAME_TAKEN")
    assert await _count_where(rollback_connection, screens, screens.c.name == name) == 1
    assert await _count_where(rollback_connection, seats) == seats_before
    assert (
        await _count_where(
            rollback_connection, seats, seats.c.screen_id == int(screen["id"])
        )
        == 6
    )


async def test_list_screens_returns_seeded_and_created(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_screen_via_api: _CreateScreen,
) -> None:
    """``GET /admin/screens`` lists stored and created screens by name (test 28).

    The list is ordered by the lowercased name and then by id (SPEC-3 Section
    2.3), so the two rows are distinguished by case: the lowercase name sorts
    before the capitalised one, which is what the two names are chosen for.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.screen.list@example.com"
    )
    seeded_id = await _insert_screen(rollback_connection, "Spec3 Seeded Screen")
    created = await create_screen_via_api(client, headers, "spec3 created screen", 2, 3)
    created_id = int(created["id"])

    response = await client.get(_ADMIN_SCREENS_URL, headers=headers)

    assert response.status_code == 200, response.text
    body = _json_list(response)
    by_id = {int(row["id"]): row for row in body}
    assert seeded_id in by_id, body
    assert created_id in by_id, body
    assert by_id[seeded_id] == {
        "id": seeded_id,
        "name": "Spec3 Seeded Screen",
        "rows": 2,
        "seats_per_row": 3,
        "seat_count": 6,
    }
    assert by_id[created_id]["seat_count"] == 6
    assert by_id[created_id]["name"] == "spec3 created screen"

    names = [(str(row["name"]).lower(), int(row["id"])) for row in body]
    assert names == sorted(names), names
    assert names.index(("spec3 created screen", created_id)) < names.index(
        ("spec3 seeded screen", seeded_id)
    )


# --- Showtime create (tests 29-35) ------------------------------------------


async def test_create_showtime_returns_201_and_utc_response(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    future_start: _FutureStart,
) -> None:
    """A created showtime comes back in UTC with its movie and screen (test 29).

    The response must carry exactly the fields SPEC-3 Section 2.3 lists -- no
    seat availability, which belongs to SPEC-4 -- with the movie and screen
    embedded from the same row, and the stored row must hold the same instant.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.showtime.create@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Showtime Movie")
    screen = await create_screen_via_api(client, headers, "Spec3 Showtime Screen", 2, 3)
    starts_at = future_start(timedelta(days=2))

    showtime = await create_showtime_via_api(
        client, headers, int(movie["id"]), int(screen["id"]), starts_at, 1234
    )

    assert set(showtime) == {"id", "starts_at", "price_cents", "movie", "screen"}, (
        showtime
    )
    assert showtime["price_cents"] == 1234
    assert showtime["movie"] == {
        "id": movie["id"],
        "title": "Spec3 Showtime Movie",
        "poster_url": None,
    }
    assert showtime["screen"] == {"id": screen["id"], "name": "Spec3 Showtime Screen"}
    _assert_utc_instant(str(showtime["starts_at"]), starts_at)

    rows = (
        await rollback_connection.execute(
            select(showtimes.c.starts_at, showtimes.c.price_cents).where(
                showtimes.c.id == int(showtime["id"])
            )
        )
    ).all()
    assert len(rows) == 1, rows
    assert rows[0].starts_at == starts_at
    assert rows[0].price_cents == 1234


async def test_create_showtime_offset_input_is_normalized_to_utc(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    future_start: _FutureStart,
) -> None:
    """A non-UTC offset is accepted and stored as the same UTC instant (test 30).

    ``starts_at`` is submitted as ``+04:00``; the response must denote the very
    same moment and express it in UTC, so a client's zone never leaks into the
    stored or returned value (FR-15).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.showtime.offset@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Offset Movie")
    screen = await create_screen_via_api(client, headers, "Spec3 Offset Screen", 2, 2)
    utc_instant = future_start(timedelta(days=3))
    submitted = utc_instant.astimezone(timezone(timedelta(hours=4)))
    payload = _showtime_payload(int(movie["id"]), int(screen["id"]), submitted, 999)
    assert "+04:00" in payload["starts_at"], payload

    response = await _post_showtime(client, headers, payload)

    assert response.status_code == 201, response.text
    body = _json_body(response)
    _assert_utc_instant(str(body["starts_at"]), utc_instant)


async def test_create_showtime_in_past_returns_422(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
) -> None:
    """``starts_at`` must be strictly in the future (FR-10, test 31).

    The service is called directly with an explicit ``now``, because that is the
    only way to pin the boundary: one second after ``now`` is accepted, while
    ``now`` itself and one second before are rejected with ``SHOWTIME_IN_PAST``
    (422). The rejected attempts must leave the table untouched.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.showtime.boundary@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Boundary Movie")
    screen = await create_screen_via_api(client, headers, "Spec3 Boundary Screen", 2, 2)
    now = datetime.now(UTC)
    accepted_start = now + timedelta(seconds=1)

    record = await service.create_showtime(
        rollback_connection,
        ShowtimeCreate(
            movie_id=int(movie["id"]),
            screen_id=int(screen["id"]),
            starts_at=accepted_start,
            price_cents=1000,
        ),
        _ACTOR_ID,
        now,
    )

    assert record.starts_at == accepted_start
    assert record.movie.id == movie["id"]
    assert record.screen.id == screen["id"]

    for rejected_start in (now, now - timedelta(seconds=1)):
        with pytest.raises(ApiError) as error:
            await service.create_showtime(
                rollback_connection,
                ShowtimeCreate(
                    movie_id=int(movie["id"]),
                    screen_id=int(screen["id"]),
                    starts_at=rejected_start,
                    price_cents=1000,
                ),
                _ACTOR_ID,
                now,
            )
        assert error.value.code == "SHOWTIME_IN_PAST"
        assert error.value.status_code == 422

    assert (
        await _count_where(
            rollback_connection, showtimes, showtimes.c.screen_id == int(screen["id"])
        )
        == 1
    )


async def test_create_showtime_unknown_movie_or_screen_returns_404(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    future_start: _FutureStart,
) -> None:
    """An unknown referenced id is a 404 and creates nothing (FR-5, test 32).

    The movie and the screen are resolved before anything is written, so both
    rejections leave the ``showtimes`` table exactly as they found it.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.showtime.unknown@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Unknown Ref Movie")
    screen = await create_screen_via_api(
        client, headers, "Spec3 Unknown Ref Screen", 2, 2
    )
    starts_at = future_start(timedelta(days=2))
    showtimes_before = await _count_where(rollback_connection, showtimes)

    unknown_movie = await _post_showtime(
        client,
        headers,
        _showtime_payload(_UNKNOWN_ID, int(screen["id"]), starts_at, 1000),
    )
    _assert_error(unknown_movie, 404, "MOVIE_NOT_FOUND")

    unknown_screen = await _post_showtime(
        client,
        headers,
        _showtime_payload(int(movie["id"]), _UNKNOWN_ID, starts_at, 1000),
    )
    _assert_error(unknown_screen, 404, "SCREEN_NOT_FOUND")

    assert await _count_where(rollback_connection, showtimes) == showtimes_before


async def test_overlap_same_screen_409_exact_slot_gap_ok_other_screen_ok(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    future_start: _FutureStart,
) -> None:
    """The overlap window is exclusive on both ends (FR-11, test 34).

    On one screen a start one minute inside the window is refused with
    ``SHOWTIME_OVERLAP``, while a start exactly one slot before or after the
    existing one is accepted; on a different screen the very same instant never
    conflicts, because the rule is per screen.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.showtime.overlap@example.com"
    )
    slot = timedelta(minutes=get_settings().showtime_slot_minutes)
    movie = await create_movie_via_api(client, headers, "Spec3 Overlap Movie")
    screen = await create_screen_via_api(client, headers, "Spec3 Overlap Screen", 2, 2)
    other_screen = await create_screen_via_api(
        client, headers, "Spec3 Overlap Other Screen", 2, 2
    )
    movie_id = int(movie["id"])
    screen_id = int(screen["id"])
    start = future_start(timedelta(days=4))

    first = await _post_showtime(
        client, headers, _showtime_payload(movie_id, screen_id, start, 1000)
    )
    assert first.status_code == 201, first.text

    too_close = await _post_showtime(
        client,
        headers,
        _showtime_payload(
            movie_id, screen_id, start + slot - timedelta(minutes=1), 1000
        ),
    )
    _assert_error(too_close, 409, "SHOWTIME_OVERLAP")

    just_before = await _post_showtime(
        client, headers, _showtime_payload(movie_id, screen_id, start - slot, 1000)
    )
    assert just_before.status_code == 201, just_before.text

    just_after = await _post_showtime(
        client, headers, _showtime_payload(movie_id, screen_id, start + slot, 1000)
    )
    assert just_after.status_code == 201, just_after.text

    other = await _post_showtime(
        client,
        headers,
        _showtime_payload(movie_id, int(other_screen["id"]), start, 1000),
    )
    assert other.status_code == 201, other.text

    assert (
        await _count_where(
            rollback_connection, showtimes, showtimes.c.screen_id == screen_id
        )
        == 3
    )
    assert (
        await _count_where(
            rollback_connection,
            showtimes,
            showtimes.c.screen_id == int(other_screen["id"]),
        )
        == 1
    )


async def test_price_boundaries(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    future_start: _FutureStart,
) -> None:
    """``price_cents`` is a strict integer in ``1..100 000 000`` (FR-10, test 35).

    Zero, a negative price and one cent above the upper bound are refused with
    the standard 422 body and create nothing, and so is a float -- money is
    always integer cents (``principles.md``). The two bounds themselves are
    accepted and echoed unchanged.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.showtime.price@example.com"
    )
    slot = timedelta(minutes=get_settings().showtime_slot_minutes)
    movie = await create_movie_via_api(client, headers, "Spec3 Price Movie")
    screen = await create_screen_via_api(client, headers, "Spec3 Price Screen", 2, 2)
    movie_id = int(movie["id"])
    screen_id = int(screen["id"])
    base = future_start(timedelta(days=5))

    for rejected_price in (0, -1, 100_000_001):
        response = await _post_showtime(
            client,
            headers,
            _showtime_payload(movie_id, screen_id, base, rejected_price),
        )
        _assert_error(response, 422, "VALIDATION_ERROR")

    float_payload = _showtime_payload(movie_id, screen_id, base, 500)
    float_payload["price_cents"] = 500.0
    _assert_error(
        await _post_showtime(client, headers, float_payload), 422, "VALIDATION_ERROR"
    )

    for index, accepted_price in enumerate((1, 100_000_000)):
        response = await _post_showtime(
            client,
            headers,
            _showtime_payload(movie_id, screen_id, base + slot * index, accepted_price),
        )
        assert response.status_code == 201, response.text
        assert _json_body(response)["price_cents"] == accepted_price

    assert (
        await _count_where(
            rollback_connection, showtimes, showtimes.c.screen_id == screen_id
        )
        == 2
    )


async def test_concurrent_overlapping_showtimes_exactly_one_succeeds(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """Two simultaneous overlapping creates: exactly one wins (FR-11, test 33).

    This is the only test in this module that COMMITS, because a race needs two
    independent transactions: only then can each request see the other's row and
    the loser be refused with ``SHOWTIME_OVERLAP`` instead of both slipping past
    the check. The two starts are 60 minutes apart on one screen, well inside the
    180-minute window, so the winner's row lock serialises them.

    Everything the test creates -- showtimes, seats, screen, movie and account --
    is deleted in ``finally`` so the shared database is left as it was found.
    """
    email = "spec3.concurrent.showtimes@example.com"
    movie_id: int | None = None
    screen_id: int | None = None
    user_id: int | None = None

    try:
        signup = await committing_client.post(
            _SIGNUP_URL, json={"email": email, "password": _USER_PASSWORD}
        )
        assert signup.status_code == 201, signup.text
        user_id = int(_json_body(signup)["id"])

        async with async_engine.begin() as connection:
            await connection.execute(
                update(users).where(users.c.id == user_id).values(role=ADMIN_USER_ROLE)
            )

        login = await committing_client.post(
            _LOGIN_URL, json={"email": email, "password": _USER_PASSWORD}
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        movie_response = await committing_client.post(
            _ADMIN_MOVIES_URL,
            json={"title": "Spec3 Concurrent Movie"},
            headers=headers,
        )
        assert movie_response.status_code == 201, movie_response.text
        movie_id = int(_json_body(movie_response)["id"])

        screen_response = await committing_client.post(
            _ADMIN_SCREENS_URL,
            json={"name": "Spec3 Concurrent Screen", "rows": 2, "seats_per_row": 2},
            headers=headers,
        )
        assert screen_response.status_code == 201, screen_response.text
        screen_id = int(_json_body(screen_response)["id"])

        start = datetime.now(UTC) + timedelta(days=2)
        payloads = [
            _showtime_payload(movie_id, screen_id, start, 1000),
            _showtime_payload(movie_id, screen_id, start + timedelta(minutes=60), 1000),
        ]

        responses = await asyncio.gather(
            committing_client.post(
                _ADMIN_SHOWTIMES_URL, json=payloads[0], headers=headers
            ),
            committing_client.post(
                _ADMIN_SHOWTIMES_URL, json=payloads[1], headers=headers
            ),
        )

        assert sorted(response.status_code for response in responses) == [201, 409], [
            (response.status_code, response.text) for response in responses
        ]
        conflict = next(
            response for response in responses if response.status_code == 409
        )
        assert _error_payload(conflict)["code"] == "SHOWTIME_OVERLAP"

        async with async_engine.connect() as connection:
            assert (
                await _count_where(
                    connection, showtimes, showtimes.c.screen_id == screen_id
                )
                == 1
            )
    finally:
        async with async_engine.begin() as connection:
            if screen_id is not None:
                await connection.execute(
                    delete(showtimes).where(showtimes.c.screen_id == screen_id)
                )
                await connection.execute(
                    delete(seats).where(seats.c.screen_id == screen_id)
                )
                await connection.execute(
                    delete(screens).where(screens.c.id == screen_id)
                )
            if movie_id is not None:
                await connection.execute(delete(movies).where(movies.c.id == movie_id))
            if user_id is not None:
                await connection.execute(
                    delete(reservations).where(reservations.c.user_id == user_id)
                )
                await connection.execute(delete(users).where(users.c.id == user_id))


# --- Showtime delete (tests 36-38) ------------------------------------------


async def test_delete_showtime_future_without_reservations_returns_204(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    future_start: _FutureStart,
) -> None:
    """A future showtime with no reservations is deleted: 204, empty (test 36).

    An unknown id is a 404 first, so the success is not mistaken for "anything
    goes"; then the delete answers 204 with no body at all (SPEC-3 Section 2.3)
    and the row is really gone.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.showtime.delete@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Delete Movie")
    screen = await create_screen_via_api(client, headers, "Spec3 Delete Screen", 2, 2)
    showtime = await create_showtime_via_api(
        client,
        headers,
        int(movie["id"]),
        int(screen["id"]),
        future_start(timedelta(days=2)),
        1500,
    )
    showtime_id = int(showtime["id"])

    missing = await client.delete(
        f"{_ADMIN_SHOWTIMES_URL}/{_UNKNOWN_ID}", headers=headers
    )
    _assert_error(missing, 404, "SHOWTIME_NOT_FOUND")

    response = await client.delete(
        f"{_ADMIN_SHOWTIMES_URL}/{showtime_id}", headers=headers
    )

    assert response.status_code == 204, response.text
    assert response.content == b"", response.text
    assert (
        await _count_where(
            rollback_connection, showtimes, showtimes.c.id == showtime_id
        )
        == 0
    )


async def test_delete_showtime_started_returns_409(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
) -> None:
    """A showtime that has already started cannot be deleted (FR-12, test 37).

    The started row can only be set up with SQL, because the API refuses a past
    ``starts_at``: that is precisely why the rule exists on delete. The refusal
    is a 409 ``SHOWTIME_STARTED`` and the row survives.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.showtime.started@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Started Movie")
    screen = await create_screen_via_api(client, headers, "Spec3 Started Screen", 2, 2)
    started_id = await _insert_showtime(
        rollback_connection,
        int(movie["id"]),
        int(screen["id"]),
        datetime.now(UTC) - timedelta(hours=1),
        1000,
    )

    response = await client.delete(
        f"{_ADMIN_SHOWTIMES_URL}/{started_id}", headers=headers
    )

    _assert_error(response, 409, "SHOWTIME_STARTED")
    assert (
        await _count_where(rollback_connection, showtimes, showtimes.c.id == started_id)
        == 1
    )


async def test_delete_showtime_with_reservation_returns_409(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    future_start: _FutureStart,
) -> None:
    """A showtime with any reservation is kept: 409 (FR-12, decision D4, test 38).

    There is no reservations API yet, so the row is inserted directly with SQL
    against a real signed-up account. The reservation keeps the showtime alive
    whatever its status, so the cancel path of SPEC-5 can never orphan history.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.showtime.reserved@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Reserved Movie")
    screen = await create_screen_via_api(client, headers, "Spec3 Reserved Screen", 2, 2)
    showtime = await create_showtime_via_api(
        client,
        headers,
        int(movie["id"]),
        int(screen["id"]),
        future_start(timedelta(days=3)),
        1200,
    )
    showtime_id = int(showtime["id"])

    signup = await create_user(
        client, "spec3.reservation.holder@example.com", _USER_PASSWORD
    )
    assert signup.status_code == 201, signup.text
    await rollback_connection.execute(
        insert(reservations).values(
            user_id=int(_json_body(signup)["id"]), showtime_id=showtime_id
        )
    )

    response = await client.delete(
        f"{_ADMIN_SHOWTIMES_URL}/{showtime_id}", headers=headers
    )

    _assert_error(response, 409, "SHOWTIME_HAS_RESERVATIONS")
    assert (
        await _count_where(
            rollback_connection, showtimes, showtimes.c.id == showtime_id
        )
        == 1
    )
    assert (
        await _count_where(
            rollback_connection,
            reservations,
            reservations.c.showtime_id == showtime_id,
        )
        == 1
    )


# --- Showtime browse (tests 39-42) ------------------------------------------


async def test_list_showtimes_by_date_filters_orders_and_embeds_movie_and_screen(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
) -> None:
    """``?date=`` selects a local day and the page is ordered (test 39).

    Three showtimes on one local day come back in start order with the movie and
    the screen embedded and the page envelope filled in (FR-14); a showtime on
    the next local day is not in the window. Because the day is given, the
    window replaces the "not yet started" rule, so an already-started showtime of
    a past local day is still browsable -- the reason that rule is worth having.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.browse.date@example.com"
    )
    zone = _cinema_zone()
    day = _local_day(zone, 5)
    movie_a = await create_movie_via_api(client, headers, "Spec3 Browse Movie A")
    movie_b = await create_movie_via_api(client, headers, "Spec3 Browse Movie B")
    screen_a = await create_screen_via_api(
        client, headers, "Spec3 Browse Screen A", 2, 2
    )
    screen_b = await create_screen_via_api(
        client, headers, "Spec3 Browse Screen B", 2, 2
    )
    movie_a_id = int(movie_a["id"])
    screen_a_id = int(screen_a["id"])

    early = await create_showtime_via_api(
        client,
        headers,
        movie_a_id,
        screen_a_id,
        _local_instant(zone, day, 10, 0),
        1000,
    )
    noon = await create_showtime_via_api(
        client,
        headers,
        int(movie_b["id"]),
        int(screen_b["id"]),
        _local_instant(zone, day, 12, 0),
        1500,
    )
    late = await create_showtime_via_api(
        client,
        headers,
        movie_a_id,
        screen_a_id,
        _local_instant(zone, day, 20, 0),
        2000,
    )
    next_day = await create_showtime_via_api(
        client,
        headers,
        movie_a_id,
        screen_a_id,
        _local_instant(zone, day + timedelta(days=1), 10, 0),
        2000,
    )

    response = await client.get(_SHOWTIMES_URL, params={"date": day.isoformat()})

    assert response.status_code == 200, response.text
    items = _assert_page(_json_body(response), total=3, limit=20, offset=0)
    assert [item["id"] for item in items] == [early["id"], noon["id"], late["id"]]
    _assert_ordered_by_starts_at(items)
    assert all(item["id"] != next_day["id"] for item in items)
    first = items[0]
    assert set(first) == {"id", "starts_at", "price_cents", "movie", "screen"}, first
    assert first["price_cents"] == 1000
    assert first["movie"] == {
        "id": movie_a_id,
        "title": "Spec3 Browse Movie A",
        "poster_url": None,
    }
    assert first["screen"] == {"id": screen_a_id, "name": "Spec3 Browse Screen A"}
    _assert_utc_instant(str(first["starts_at"]), _local_instant(zone, day, 10, 0))

    # a day with nothing on it is an empty page, not a 404
    empty = await client.get(
        _SHOWTIMES_URL, params={"date": (day + timedelta(days=30)).isoformat()}
    )
    assert empty.status_code == 200, empty.text
    assert _assert_page(_json_body(empty), total=0, limit=20, offset=0) == []

    # a past local day is browsable too, even though the showtime has started
    past_day = _local_day(zone, -3)
    started_id = await _insert_showtime(
        rollback_connection,
        movie_a_id,
        screen_a_id,
        _local_instant(zone, past_day, 21, 0),
        900,
    )
    past = await client.get(_SHOWTIMES_URL, params={"date": past_day.isoformat()})
    assert past.status_code == 200, past.text
    assert [
        item["id"]
        for item in _assert_page(_json_body(past), total=1, limit=20, offset=0)
    ] == [started_id]


async def test_list_showtimes_date_uses_cinema_timezone_not_utc(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
) -> None:
    """``?date=`` is a day in ``CINEMA_TIMEZONE``, not a UTC day (FR-13, test 40).

    Two showtimes 60 minutes apart, at 23:30 local on day D and 00:30 local on
    D+1, land on the SAME UTC calendar date (19:30Z and 20:30Z with the default
    ``Asia/Tbilisi``), so a filter that compared UTC dates could not tell them
    apart. The local window can: each date returns exactly its own showtime.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.browse.local.day@example.com"
    )
    zone = _cinema_zone()
    day = _local_day(zone, 4)
    next_day = day + timedelta(days=1)
    movie = await create_movie_via_api(client, headers, "Spec3 Local Day Movie")
    screen = await create_screen_via_api(
        client, headers, "Spec3 Local Day Screen", 2, 2
    )
    other_screen = await create_screen_via_api(
        client, headers, "Spec3 Local Day Other Screen", 2, 2
    )
    movie_id = int(movie["id"])
    late_local = _local_instant(zone, day, 23, 30)
    after_midnight_local = _local_instant(zone, next_day, 0, 30)

    # The trap this test exists for: the two instants share one UTC date.
    assert late_local.date() == after_midnight_local.date(), (
        late_local,
        after_midnight_local,
    )

    # Two screens, because 60 minutes apart on one screen is an overlap (FR-11).
    late = await create_showtime_via_api(
        client, headers, movie_id, int(screen["id"]), late_local, 1000
    )
    after_midnight = await create_showtime_via_api(
        client,
        headers,
        movie_id,
        int(other_screen["id"]),
        after_midnight_local,
        1000,
    )

    on_day = await client.get(_SHOWTIMES_URL, params={"date": day.isoformat()})
    assert on_day.status_code == 200, on_day.text
    assert [
        item["id"]
        for item in _assert_page(_json_body(on_day), total=1, limit=20, offset=0)
    ] == [late["id"]]

    on_next_day = await client.get(
        _SHOWTIMES_URL, params={"date": next_day.isoformat()}
    )
    assert on_next_day.status_code == 200, on_next_day.text
    assert [
        item["id"]
        for item in _assert_page(_json_body(on_next_day), total=1, limit=20, offset=0)
    ] == [after_midnight["id"]]


async def test_list_showtimes_without_date_returns_only_future(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
) -> None:
    """Without ``date``, only showtimes that have not started (FR-13, test 41).

    The rule is ``starts_at >= now``, inclusive, so it is checked with an
    explicit ``now`` at the service: the showtime exactly at ``now`` is listed
    and the one an hour earlier is not. Three rows are written with SQL because
    two of them must be in the past to be interesting, and the HTTP request
    afterwards shows the route passes the real clock in.
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.browse.future@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Future Movie")
    screen = await create_screen_via_api(client, headers, "Spec3 Future Screen", 2, 2)
    movie_id = int(movie["id"])
    screen_id = int(screen["id"])
    pinned = datetime.now(UTC)

    past_id = await _insert_showtime(
        rollback_connection, movie_id, screen_id, pinned - timedelta(hours=1), 1000
    )
    boundary_id = await _insert_showtime(
        rollback_connection, movie_id, screen_id, pinned, 1000
    )
    future_id = await _insert_showtime(
        rollback_connection, movie_id, screen_id, pinned + timedelta(hours=1), 1000
    )

    records, total = await service.list_showtimes(
        rollback_connection,
        day=None,
        movie_id=movie_id,
        limit=20,
        offset=0,
        now=pinned,
    )

    assert total == 2, [record.id for record in records]
    assert [record.id for record in records] == [boundary_id, future_id]
    assert all(record.id != past_id for record in records)

    response = await client.get(_SHOWTIMES_URL, params={"movie_id": movie_id})

    assert response.status_code == 200, response.text
    items = _assert_page(_json_body(response), total=1, limit=20, offset=0)
    assert [item["id"] for item in items] == [future_id]


async def test_list_showtimes_movie_filter_pagination_and_bad_date_422(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
) -> None:
    """The movie filter, the page window and the rejected query values (test 42).

    ``movie_id`` narrows the browse to one movie without hiding the others'
    rows, an unknown id simply matches nothing, and ``limit``/``offset`` window
    the matches while ``total`` keeps counting all of them (FR-14). The bounds
    1 and 100 are accepted; ``limit`` 0 or 101, ``offset`` -1 and a malformed
    ``date`` are rejected by FastAPI with the standard 422 body (FR-14, FR-15).
    """
    headers = await admin_headers(
        client, rollback_connection, "spec3.browse.page@example.com"
    )
    zone = _cinema_zone()
    day = _local_day(zone, 6)
    movie_a = await create_movie_via_api(client, headers, "Spec3 Page Movie A")
    movie_b = await create_movie_via_api(client, headers, "Spec3 Page Movie B")
    screen = await create_screen_via_api(client, headers, "Spec3 Page Screen", 2, 2)
    movie_a_id = int(movie_a["id"])
    screen_id = int(screen["id"])

    page_ids = [
        int(
            (
                await create_showtime_via_api(
                    client,
                    headers,
                    movie_a_id,
                    screen_id,
                    _local_instant(zone, day, hour, 0),
                    1000 + index,
                )
            )["id"]
        )
        for index, hour in enumerate((9, 12, 15))
    ]
    other_movie = await create_showtime_via_api(
        client,
        headers,
        int(movie_b["id"]),
        screen_id,
        _local_instant(zone, day, 18, 0),
        2000,
    )
    other_movie_id = int(other_movie["id"])

    filtered = await client.get(_SHOWTIMES_URL, params={"movie_id": movie_a_id})

    assert filtered.status_code == 200, filtered.text
    items = _assert_page(_json_body(filtered), total=3, limit=20, offset=0)
    assert [item["id"] for item in items] == page_ids
    _assert_ordered_by_starts_at(items)
    assert all(item["movie"]["id"] == movie_a_id for item in items)
    assert all(item["id"] != other_movie_id for item in items)

    first_page = await client.get(
        _SHOWTIMES_URL, params={"movie_id": movie_a_id, "limit": 2}
    )
    assert first_page.status_code == 200, first_page.text
    assert [
        item["id"]
        for item in _assert_page(_json_body(first_page), total=3, limit=2, offset=0)
    ] == page_ids[:2]

    second_page = await client.get(
        _SHOWTIMES_URL, params={"movie_id": movie_a_id, "limit": 2, "offset": 2}
    )
    assert second_page.status_code == 200, second_page.text
    assert [
        item["id"]
        for item in _assert_page(_json_body(second_page), total=3, limit=2, offset=2)
    ] == page_ids[2:]

    unknown = await client.get(_SHOWTIMES_URL, params={"movie_id": _UNKNOWN_ID})
    assert unknown.status_code == 200, unknown.text
    assert _assert_page(_json_body(unknown), total=0, limit=20, offset=0) == []

    for accepted_limit in (1, 100):
        accepted = await client.get(
            _SHOWTIMES_URL, params={"movie_id": movie_a_id, "limit": accepted_limit}
        )
        assert accepted.status_code == 200, accepted.text
        accepted_items = _assert_page(
            _json_body(accepted), total=3, limit=accepted_limit, offset=0
        )
        assert len(accepted_items) == min(accepted_limit, 3)

    # A midnight timestamp such as ``2026-10-16T00:00:00`` is deliberately not in
    # this list: Pydantic accepts it as the same calendar date, so it is a 200.
    for name, rejected in (
        ("limit_below", {"limit": 0}),
        ("limit_above", {"limit": 101}),
        ("limit_not_a_number", {"limit": "abc"}),
        ("offset_negative", {"offset": -1}),
        ("date_not_a_date", {"date": "not-a-date"}),
        ("date_impossible", {"date": "2026-13-01"}),
    ):
        response = await client.get(_SHOWTIMES_URL, params=rejected)
        assert response.status_code == 422, (name, response.text)
        assert _error_payload(response)["code"] == "VALIDATION_ERROR", (
            name,
            response.text,
        )
