"""Integration tests for the genre and movie endpoints (SPEC-3 tests 8-24).

These tests drive the real application through an httpx ``ASGITransport`` client
against a real PostgreSQL server (``TEST_DATABASE_URL``); every request of one
test shares ONE connection inside an outer transaction that is rolled back at
teardown, so nothing here is committed and no test can observe another one's rows
(``tests/conftest.py``). The setup they share -- an acting administrator, a
movie, a screen and a showtime -- comes from the SPEC-3 fixtures in
``conftest.py``.

What each test pins down, by requirement:

* 8  FR-1  every public read answers without a token;
* 9  FR-2  every ``/admin`` route answers 401 anonymously and 403 to a user;
* 10 FR-16 each SPEC-3 failure code is rendered in the standard body shape;
* 11-13 FR-8 genre create and delete: trimming, case-insensitive uniqueness,
        and the "in use" refusal;
* 14-21 FR-3, FR-4, FR-5, FR-6, FR-7 movie create, update and delete;
* 22-24 FR-14, FR-5 the movie list's page envelope, order and filters.

Every failure case asserts the status, the error code, and that nothing was
written, so a rejected request can never half-apply.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from src.db.tables import (
    genres,
    movie_genres,
    movies,
    screens,
    showtimes,
)

# The endpoints under test (SPEC-3 Section 2.3).
_GENRES_URL = "/genres"
_MOVIES_URL = "/movies"
_SHOWTIMES_URL = "/showtimes"
_ADMIN_GENRES_URL = "/admin/genres"
_ADMIN_MOVIES_URL = "/admin/movies"
_ADMIN_SCREENS_URL = "/admin/screens"
_ADMIN_SHOWTIMES_URL = "/admin/showtimes"

# The id used wherever a row must not exist. Far above anything this suite could
# realistically generate, so a 404 here can never be an existing row.
_UNKNOWN_ID = 999_999_999

# A throwaway password for the accounts these tests sign up. Ordinary test data,
# never a credential (IMPLEMENTER RULES, rule 6).
_USER_PASSWORD = "spec3 user password"


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

    Used where the endpoint answers with a bare list rather than a page
    envelope: ``GET /genres`` and ``GET /admin/screens``.

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


async def _count_rows(conn: AsyncConnection, table: Table) -> int:
    """Count every row of a table.

    Args:
        conn: The test's connection, inside the same transaction as the
            application's requests.
        table: The table to count.

    Returns:
        int: The number of rows.
    """
    return int(
        (await conn.execute(select(func.count()).select_from(table))).scalar_one()
    )


async def _count_named(conn: AsyncConnection, table: Table, name: str) -> int:
    """Count the rows of a table whose ``name`` equals ``name`` ignoring case.

    Args:
        conn: The test's connection.
        table: A table with a ``name`` column (``genres`` or ``screens``).
        name: The name to look for.

    Returns:
        int: The number of matching rows.
    """
    return int(
        (
            await conn.execute(
                select(func.count())
                .select_from(table)
                .where(func.lower(table.c.name) == name.lower())
            )
        ).scalar_one()
    )


async def _count_genre_links(conn: AsyncConnection, movie_id: int) -> int:
    """Count the ``movie_genres`` rows of one movie.

    Args:
        conn: The test's connection.
        movie_id: The movie's primary key.

    Returns:
        int: The number of genre links.
    """
    return int(
        (
            await conn.execute(
                select(func.count())
                .select_from(movie_genres)
                .where(movie_genres.c.movie_id == movie_id)
            )
        ).scalar_one()
    )


async def _count_titled(conn: AsyncConnection, title: str) -> int:
    """Count the ``movies`` rows whose ``title`` equals ``title`` ignoring case.

    Args:
        conn: The test's connection.
        title: The title to look for.

    Returns:
        int: The number of matching rows.
    """
    return int(
        (
            await conn.execute(
                select(func.count())
                .select_from(movies)
                .where(func.lower(movies.c.title) == title.lower())
            )
        ).scalar_one()
    )


async def _create_genre(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    name: str,
) -> dict[str, Any]:
    """Create a genre through ``POST /admin/genres`` (SPEC-3 FR-8).

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting administrator's ``Authorization`` header.
        name: The genre name, submitted exactly as given so a test can prove the
            API trims it.

    Returns:
        dict[str, Any]: The 201 response body, i.e. a ``GenreOut``.

    Raises:
        AssertionError: If the genre is not created, with the body attached.
    """
    response = await client.post("/admin/genres", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return _json_body(response)


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
_AuthHeader = Callable[[str], dict[str, str]]


async def test_public_reads_need_no_token(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    future_start: _FutureStart,
) -> None:
    """Test 8 (FR-1): every catalogue read answers 200 without a token."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.public.reads@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Public Reads Movie")
    screen = await create_screen_via_api(
        client, headers, "Spec3 Public Reads Screen", 2, 3
    )
    showtime = await create_showtime_via_api(
        client,
        headers,
        movie["id"],
        screen["id"],
        future_start(timedelta(days=1)),
        1500,
    )
    movie_id = movie["id"]
    showtime_id = showtime["id"]

    for path in (
        _GENRES_URL,
        _MOVIES_URL,
        f"{_MOVIES_URL}/{movie_id}",
        _SHOWTIMES_URL,
        f"{_SHOWTIMES_URL}/{showtime_id}",
    ):
        response = await client.get(path)
        assert response.status_code == 200, (path, response.text)

    # The id-addressed reads really return the rows, not just a 200.
    assert _json_body(await client.get(f"{_MOVIES_URL}/{movie_id}"))["id"] == movie_id
    assert (
        _json_body(await client.get(f"{_SHOWTIMES_URL}/{showtime_id}"))["id"]
        == showtime_id
    )


async def test_admin_writes_return_401_anonymous_and_403_for_user(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
    future_start: _FutureStart,
) -> None:
    """Test 9 (FR-2): every ``/admin`` route is 401 anonymous, 403 for a user."""
    admin = await admin_headers(
        client, rollback_connection, "spec3.role.admin@example.com"
    )
    genre = await _create_genre(client, admin, "Spec3 Role Genre")
    movie = await create_movie_via_api(client, admin, "Spec3 Role Movie")
    screen = await create_screen_via_api(client, admin, "Spec3 Role Screen", 1, 1)
    showtime = await create_showtime_via_api(
        client,
        admin,
        movie["id"],
        screen["id"],
        future_start(timedelta(days=2)),
        900,
    )

    viewer_signup = await create_user(
        client, "spec3.role.viewer@example.com", _USER_PASSWORD
    )
    assert viewer_signup.status_code == 201, viewer_signup.text
    viewer = auth_header(
        await login(client, "spec3.role.viewer@example.com", _USER_PASSWORD)
    )

    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("POST", _ADMIN_GENRES_URL, {"name": "Spec3 Role Forbidden Genre"}),
        ("DELETE", f"{_ADMIN_GENRES_URL}/{genre['id']}", None),
        ("POST", _ADMIN_MOVIES_URL, {"title": "Spec3 Role Forbidden Movie"}),
        ("PATCH", f"{_ADMIN_MOVIES_URL}/{movie['id']}", {"title": "Spec3 Renamed"}),
        ("DELETE", f"{_ADMIN_MOVIES_URL}/{movie['id']}", None),
        ("GET", _ADMIN_SCREENS_URL, None),
        (
            "POST",
            _ADMIN_SCREENS_URL,
            {"name": "Spec3 Role Forbidden Screen", "rows": 1, "seats_per_row": 1},
        ),
        (
            "POST",
            _ADMIN_SHOWTIMES_URL,
            {
                "movie_id": movie["id"],
                "screen_id": screen["id"],
                "starts_at": future_start(timedelta(days=3)).isoformat(),
                "price_cents": 1000,
            },
        ),
        ("DELETE", f"{_ADMIN_SHOWTIMES_URL}/{showtime['id']}", None),
    ]

    counts = {
        "genres": await _count_rows(rollback_connection, genres),
        "movies": await _count_rows(rollback_connection, movies),
        "screens": await _count_rows(rollback_connection, screens),
        "showtimes": await _count_rows(rollback_connection, showtimes),
    }

    for method, path, body in calls:
        anonymous = await client.request(method, path, json=body)
        _assert_error(anonymous, 401, "UNAUTHENTICATED")
        forbidden = await client.request(method, path, json=body, headers=viewer)
        _assert_error(forbidden, 403, "FORBIDDEN")

    # Neither the anonymous nor the forbidden call wrote anything (FR-2, FR-4).
    assert await _count_rows(rollback_connection, genres) == counts["genres"]
    assert await _count_rows(rollback_connection, movies) == counts["movies"]
    assert await _count_rows(rollback_connection, screens) == counts["screens"]
    assert await _count_rows(rollback_connection, showtimes) == counts["showtimes"]


async def test_error_bodies_use_standard_shape_for_new_codes(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    future_start: _FutureStart,
) -> None:
    """Test 10 (Section 2.9): every SPEC-3 error uses the shared envelope."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.shape@example.com"
    )
    genre = await _create_genre(client, headers, "Spec3 Shape Genre")
    movie = await create_movie_via_api(
        client, headers, "Spec3 Shape Movie", [genre["id"]]
    )
    screen = await create_screen_via_api(client, headers, "Spec3 Shape Screen", 1, 1)
    showtime = await create_showtime_via_api(
        client,
        headers,
        movie["id"],
        screen["id"],
        future_start(timedelta(days=4)),
        700,
    )
    assert showtime["id"] > 0

    # One request per new error code, each checked against the exact shape.
    _assert_error(
        await client.post(
            _ADMIN_MOVIES_URL,
            json={"title": "Spec3 Shape Ghost Movie", "genre_ids": [_UNKNOWN_ID]},
            headers=headers,
        ),
        404,
        "GENRE_NOT_FOUND",
    )
    _assert_error(
        await client.get(f"{_MOVIES_URL}/{_UNKNOWN_ID}"), 404, "MOVIE_NOT_FOUND"
    )
    _assert_error(
        await client.post(
            _ADMIN_GENRES_URL, json={"name": "spec3 shape genre"}, headers=headers
        ),
        409,
        "GENRE_ALREADY_EXISTS",
    )
    _assert_error(
        await client.delete(f"{_ADMIN_GENRES_URL}/{genre['id']}", headers=headers),
        409,
        "GENRE_IN_USE",
    )
    _assert_error(
        await client.delete(f"{_ADMIN_MOVIES_URL}/{movie['id']}", headers=headers),
        409,
        "MOVIE_HAS_SHOWTIMES",
    )
    _assert_error(
        await client.post(
            _ADMIN_GENRES_URL,
            json={"name": "Spec3 Shape Extra Genre", "extra": True},
            headers=headers,
        ),
        422,
        "VALIDATION_ERROR",
    )
    # FastAPI's own 404 keeps the same envelope, so a client never has to parse
    # two different error bodies (SPEC-2 Section 2.9).
    _assert_error(await client.get("/spec3-no-such-route"), 404, "NOT_FOUND")


async def test_create_genre_trims_and_lists_sorted(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 11 (FR-8, FR-9): names are trimmed and listed in ``lower(name)`` order."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.genre.list@example.com"
    )
    created = [
        _json_body(
            await client.post(_ADMIN_GENRES_URL, json={"name": name}, headers=headers)
        )
        for name in ("  Zeta  ", "alpha", "Beta")
    ]

    # The 201 body carries the trimmed name.
    assert [genre["name"] for genre in created] == ["Zeta", "alpha", "Beta"]

    body = _json_list(await client.get(_GENRES_URL))
    names = [genre["name"] for genre in body]
    assert {genre["id"] for genre in created} <= {genre["id"] for genre in body}
    # Only the three names this test created, in ``lower(name)`` order, so a
    # database seeded with other genres does not make this assertion brittle.
    mine = [name for name in names if name.lower() in {"alpha", "beta", "zeta"}]
    assert mine == ["alpha", "Beta", "Zeta"]
    assert "  Zeta  " not in names


async def test_create_genre_duplicate_case_insensitive_returns_409(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 12 (FR-8): a name already taken, ignoring case, is a 409."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.genre.duplicate@example.com"
    )
    before = await _count_rows(rollback_connection, genres)
    first = await _create_genre(client, headers, "Spec3 Drama")

    for duplicate in ("spec3 drama", "  SPEC3 DRAMA  ", "Spec3 Drama"):
        response = await client.post(
            _ADMIN_GENRES_URL, json={"name": duplicate}, headers=headers
        )
        _assert_error(response, 409, "GENRE_ALREADY_EXISTS")

    # Exactly one row, and it is the trimmed original.
    assert await _count_rows(rollback_connection, genres) == before + 1
    assert await _count_named(rollback_connection, genres, "Spec3 Drama") == 1
    assert first["name"] == "Spec3 Drama"


async def test_delete_genre_in_use_returns_409_and_unused_returns_204(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 13 (FR-8): a genre is only removable once no movie uses it."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.genre.delete@example.com"
    )
    in_use = await _create_genre(client, headers, "Spec3 Genre In Use")
    unused = await _create_genre(client, headers, "Spec3 Genre Unused")
    movie = await create_movie_via_api(
        client, headers, "Spec3 Genre Delete Movie", [in_use["id"]]
    )

    blocked = await client.delete(
        f"{_ADMIN_GENRES_URL}/{in_use['id']}", headers=headers
    )
    _assert_error(blocked, 409, "GENRE_IN_USE")
    # Neither the genre nor its link to the movie went away.
    assert await _count_named(rollback_connection, genres, "Spec3 Genre In Use") == 1
    links = await _count_genre_links(rollback_connection, movie["id"])
    assert links == 1

    removed = await client.delete(
        f"{_ADMIN_GENRES_URL}/{unused['id']}", headers=headers
    )
    assert removed.status_code == 204, removed.text
    assert removed.content == b""
    assert await _count_named(rollback_connection, genres, "Spec3 Genre Unused") == 0

    missing = await client.delete(
        f"{_ADMIN_GENRES_URL}/{unused['id']}", headers=headers
    )
    _assert_error(missing, 404, "GENRE_NOT_FOUND")

    # The movie kept its genre, so the link count is unchanged.
    assert await _count_genre_links(rollback_connection, movie["id"]) == links


async def test_create_movie_with_genres_returns_201(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 14 (FR-3): a movie is created with its genres and echoed back."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.movie.create@example.com"
    )
    drama = await _create_genre(client, headers, "Spec3 Create Drama")
    comedy = await _create_genre(client, headers, "Spec3 Create Comedy")

    response = await client.post(
        _ADMIN_MOVIES_URL,
        json={
            "title": "  Spec3 Created Movie  ",
            "description": "  A movie created by an integration test.  ",
            "poster_url": "  https://example.test/spec3-poster.jpg  ",
            # Deliberately not in name order: the response is sorted by name.
            "genre_ids": [drama["id"], comedy["id"]],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = _json_body(response)

    # Exactly the ``MovieOut`` fields, so no internal column can leak out.
    assert set(body) == {
        "id",
        "title",
        "description",
        "poster_url",
        "genres",
        "created_at",
        "updated_at",
    }
    assert body["title"] == "Spec3 Created Movie"
    assert body["description"] == "A movie created by an integration test."
    assert body["poster_url"] == "https://example.test/spec3-poster.jpg"
    for genre in body["genres"]:
        assert set(genre) == {"id", "name"}
    assert [(genre["id"], genre["name"]) for genre in body["genres"]] == [
        (comedy["id"], "Spec3 Create Comedy"),
        (drama["id"], "Spec3 Create Drama"),
    ]
    assert _parse_instant(body["created_at"]) <= _parse_instant(body["updated_at"])

    # One row and exactly two links, both of them the requested genres.
    assert await _count_titled(rollback_connection, "Spec3 Created Movie") == 1
    assert await _count_genre_links(rollback_connection, body["id"]) == 2


async def test_create_movie_unknown_genre_returns_404_and_creates_nothing(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 15 (FR-5): a genre id that does not exist fails the whole create."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.movie.unknown.genre@example.com"
    )
    known = await _create_genre(client, headers, "Spec3 Known Genre")
    movies_before = await _count_rows(rollback_connection, movies)
    links_before = await _count_rows(rollback_connection, movie_genres)

    response = await client.post(
        _ADMIN_MOVIES_URL,
        json={
            "title": "Spec3 Movie With Ghost Genre",
            "genre_ids": [known["id"], _UNKNOWN_ID],
        },
        headers=headers,
    )
    _assert_error(response, 404, "GENRE_NOT_FOUND")

    # The whole create was rejected: no movie, and no link to the known genre.
    assert await _count_rows(rollback_connection, movies) == movies_before
    assert await _count_rows(rollback_connection, movie_genres) == links_before
    assert await _count_titled(rollback_connection, "Spec3 Movie With Ghost Genre") == 0


async def test_create_movie_validation_failures_return_422(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 16 (FR-4): every rejected body is a 422 and writes nothing."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.movie.validation@example.com"
    )
    genre = await _create_genre(client, headers, "Spec3 Validation Genre")
    movies_before = await _count_rows(rollback_connection, movies)
    links_before = await _count_rows(rollback_connection, movie_genres)

    rejected: list[dict[str, Any]] = [
        # title: required, non-empty, at most 200 characters
        {},
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"title": None},
        # genre_ids: at most 10, no duplicates
        {"title": "Spec3 Validation Movie", "genre_ids": [genre["id"], genre["id"]]},
        {
            "title": "Spec3 Validation Movie",
            "genre_ids": list(range(_UNKNOWN_ID, _UNKNOWN_ID + 11)),
        },
        # description: at most 5000 characters
        {"title": "Spec3 Validation Movie", "description": "d" * 5001},
        # poster_url: absolute http/https only
        {"title": "Spec3 Validation Movie", "poster_url": "ftp://example.test/a.jpg"},
        {"title": "Spec3 Validation Movie", "poster_url": "/spec3-poster.jpg"},
        {"title": "Spec3 Validation Movie", "poster_url": "not a url"},
        {"title": "Spec3 Validation Movie", "poster_url": ""},
        # unknown fields are rejected rather than ignored
        {"title": "Spec3 Validation Movie", "runtime_minutes": 90},
    ]

    for body in rejected:
        response = await client.post(_ADMIN_MOVIES_URL, json=body, headers=headers)
        assert response.status_code == 422, (body, response.text)
        _assert_error(response, 422, "VALIDATION_ERROR")

    # Nothing was written by any of the rejected requests.
    assert await _count_rows(rollback_connection, movies) == movies_before
    assert await _count_rows(rollback_connection, movie_genres) == links_before

    # The two boundaries that must be accepted (SPEC-3 Section 2.4).
    for body in (
        {"title": "y" * 200, "genre_ids": [genre["id"]]},
        {"title": "Spec3 Empty Description Movie", "description": ""},
    ):
        response = await client.post(_ADMIN_MOVIES_URL, json=body, headers=headers)
        assert response.status_code == 201, (body, response.text)


async def test_patch_movie_updates_only_given_fields_and_bumps_updated_at(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 17 (FR-6): a partial update changes only what was sent."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.movie.patch@example.com"
    )
    genre = await _create_genre(client, headers, "Spec3 Patch Genre")
    created = _json_body(
        await client.post(
            _ADMIN_MOVIES_URL,
            json={
                "title": "Spec3 Patch Original",
                "description": "The original description.",
                "poster_url": "https://example.test/spec3-original.jpg",
                "genre_ids": [genre["id"]],
            },
            headers=headers,
        )
    )
    movie_id = created["id"]

    response = await client.patch(
        f"{_ADMIN_MOVIES_URL}/{movie_id}",
        json={"title": "  Spec3 Patch Renamed  "},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = _json_body(response)

    assert body["id"] == movie_id
    assert body["title"] == "Spec3 Patch Renamed"
    # Everything that was not sent is exactly as it was.
    assert body["description"] == created["description"]
    assert body["poster_url"] == created["poster_url"]
    assert body["genres"] == created["genres"]
    assert body["created_at"] == created["created_at"]
    # ``updated_at`` moved forward (the service stamps it with the clock).
    assert _parse_instant(body["updated_at"]) > _parse_instant(created["updated_at"])

    row = (
        await rollback_connection.execute(
            select(movies.c.title, movies.c.description, movies.c.poster_url).where(
                movies.c.id == movie_id
            )
        )
    ).one()
    assert row.title == "Spec3 Patch Renamed"
    assert row.description == created["description"]
    assert row.poster_url == created["poster_url"]

    _assert_error(
        await client.patch(
            f"{_ADMIN_MOVIES_URL}/{_UNKNOWN_ID}",
            json={"title": "Spec3 Ghost Patch"},
            headers=headers,
        ),
        404,
        "MOVIE_NOT_FOUND",
    )


async def test_patch_movie_replaces_genre_set_and_null_clears_poster(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 18 (FR-6, FR-5): ``genre_ids`` replaces the set; ``null`` clears."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.movie.replace@example.com"
    )
    first = await _create_genre(client, headers, "Spec3 Replace First")
    second = await _create_genre(client, headers, "Spec3 Replace Second")
    third = await _create_genre(client, headers, "Spec3 Replace Third")
    created = _json_body(
        await client.post(
            _ADMIN_MOVIES_URL,
            json={
                "title": "Spec3 Replace Movie",
                "poster_url": "https://example.test/spec3-replace.jpg",
                "genre_ids": [first["id"], second["id"]],
            },
            headers=headers,
        )
    )
    movie_id = created["id"]
    url = f"{_ADMIN_MOVIES_URL}/{movie_id}"
    assert await _count_genre_links(rollback_connection, movie_id) == 2

    # A new set replaces the old one wholesale rather than being added to it.
    replaced = await client.patch(
        url, json={"genre_ids": [third["id"]]}, headers=headers
    )
    assert replaced.status_code == 200, replaced.text
    body = _json_body(replaced)
    assert [(g["id"], g["name"]) for g in body["genres"]] == [
        (third["id"], "Spec3 Replace Third")
    ]
    assert await _count_genre_links(rollback_connection, movie_id) == 1
    assert body["poster_url"] == created["poster_url"]

    # An unknown genre in the new set is refused and the links are untouched.
    blocked = await client.patch(
        url, json={"genre_ids": [_UNKNOWN_ID]}, headers=headers
    )
    _assert_error(blocked, 404, "GENRE_NOT_FOUND")
    assert await _count_genre_links(rollback_connection, movie_id) == 1

    # An empty list is how a client clears every genre.
    emptied = await client.patch(url, json={"genre_ids": []}, headers=headers)
    assert emptied.status_code == 200, emptied.text
    assert _json_body(emptied)["genres"] == []
    assert await _count_genre_links(rollback_connection, movie_id) == 0
    # The genres themselves are untouched: only the links went away.
    for name in ("Spec3 Replace First", "Spec3 Replace Second", "Spec3 Replace Third"):
        assert await _count_named(rollback_connection, genres, name) == 1

    # ``null`` is how a client clears the poster.
    cleared = await client.patch(url, json={"poster_url": None}, headers=headers)
    assert cleared.status_code == 200, cleared.text
    cleared_body = _json_body(cleared)
    assert "poster_url" in cleared_body
    assert cleared_body["poster_url"] is None
    assert cleared_body["title"] == created["title"]


async def test_patch_movie_empty_body_returns_422_and_unknown_id_404(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 19 (FR-4, FR-5): an empty update is a 422; a missing movie a 404."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.movie.patch.empty@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Patch Empty Movie")
    url = f"{_ADMIN_MOVIES_URL}/{movie['id']}"
    movies_before = await _count_rows(rollback_connection, movies)

    rejected: list[dict[str, Any]] = [
        # ``{}`` changes nothing, which the model rejects outright (FR-4).
        {},
        {"title": None},
        {"description": None},
        {"genre_ids": None},
        {"poster_url": 12345},
        {"title": "  "},
        {"runtime_minutes": 90},
    ]
    for body in rejected:
        response = await client.patch(url, json=body, headers=headers)
        assert response.status_code == 422, (body, response.text)
        _assert_error(response, 422, "VALIDATION_ERROR")

    # Every rejected update left the row exactly as it was.
    assert await _count_rows(rollback_connection, movies) == movies_before
    assert _json_body(await client.get(f"{_MOVIES_URL}/{movie['id']}")) == movie

    _assert_error(
        await client.patch(
            f"{_ADMIN_MOVIES_URL}/{_UNKNOWN_ID}",
            json={"title": "Spec3 Ghost Update"},
            headers=headers,
        ),
        404,
        "MOVIE_NOT_FOUND",
    )


async def test_delete_movie_without_showtimes_returns_204_and_removes_genre_links(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 20 (FR-7): deleting is allowed, and takes the genre links with it."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.movie.delete@example.com"
    )
    genre = await _create_genre(client, headers, "Spec3 Delete Genre")
    movie = await create_movie_via_api(
        client, headers, "Spec3 Delete Movie", [genre["id"]]
    )
    movie_id = movie["id"]
    assert await _count_genre_links(rollback_connection, movie_id) == 1

    response = await client.delete(f"{_ADMIN_MOVIES_URL}/{movie_id}", headers=headers)
    assert response.status_code == 204, response.text
    assert response.content == b""

    # The movie and its links are gone; the genre it used is not (FR-7).
    assert await _count_titled(rollback_connection, "Spec3 Delete Movie") == 0
    assert await _count_genre_links(rollback_connection, movie_id) == 0
    assert await _count_named(rollback_connection, genres, "Spec3 Delete Genre") == 1

    _assert_error(
        await client.delete(f"{_ADMIN_MOVIES_URL}/{movie_id}", headers=headers),
        404,
        "MOVIE_NOT_FOUND",
    )


async def test_delete_movie_with_showtime_returns_409(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    create_screen_via_api: _CreateScreen,
    create_showtime_via_api: _CreateShowtime,
    future_start: _FutureStart,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 21 (FR-7): a movie with a showtime is refused until it has none."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.movie.delete.blocked@example.com"
    )
    movie = await create_movie_via_api(client, headers, "Spec3 Movie With Showtime")
    screen = await create_screen_via_api(
        client, headers, "Spec3 Movie Showtime Screen", 2, 2
    )
    showtimes_before = await _count_rows(rollback_connection, showtimes)
    showtime = await create_showtime_via_api(
        client,
        headers,
        movie["id"],
        screen["id"],
        future_start(timedelta(days=5)),
        800,
    )

    blocked = await client.delete(f"{_ADMIN_MOVIES_URL}/{movie['id']}", headers=headers)
    _assert_error(blocked, 409, "MOVIE_HAS_SHOWTIMES")
    # The refusal wrote nothing.
    assert await _count_titled(rollback_connection, "Spec3 Movie With Showtime") == 1
    assert await _count_rows(rollback_connection, showtimes) == showtimes_before + 1

    # With its only showtime deleted the movie can go (Section 2.6).
    removed_showtime = await client.delete(
        f"{_ADMIN_SHOWTIMES_URL}/{showtime['id']}", headers=headers
    )
    assert removed_showtime.status_code == 204, removed_showtime.text
    removed = await client.delete(f"{_ADMIN_MOVIES_URL}/{movie['id']}", headers=headers)
    assert removed.status_code == 204, removed.text
    assert await _count_titled(rollback_connection, "Spec3 Movie With Showtime") == 0


async def test_list_movies_pagination_total_and_order(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 22 (FR-14): the list is a page envelope in ``lower(title)`` order."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.movie.list@example.com"
    )
    movies_before = await _count_rows(rollback_connection, movies)
    created = [
        _json_body(
            await client.post(_ADMIN_MOVIES_URL, json={"title": title}, headers=headers)
        )
        for title in ("Spec3 List Bravo", "Spec3 List Alpha", "Spec3 List Charlie")
    ]

    # One big page gives the whole list, whatever else is in the database.
    whole = _json_body(
        await client.get(_MOVIES_URL, params={"limit": 100, "offset": 0})
    )
    assert set(whole) == {"items", "total", "limit", "offset"}
    assert whole["limit"] == 100
    assert whole["offset"] == 0
    assert whole["total"] == movies_before + 3
    assert whole["total"] == len(whole["items"])

    titles = [item["title"] for item in whole["items"]]
    assert titles == sorted(titles, key=str.lower)
    mine = [title for title in titles if title.startswith("Spec3 List ")]
    assert mine == ["Spec3 List Alpha", "Spec3 List Bravo", "Spec3 List Charlie"]
    # A list item is the same ``MovieOut`` shape as a single read.
    created_ids = {movie["id"] for movie in created}
    for item in whole["items"]:
        if item["id"] in created_ids:
            assert set(item) == {
                "id",
                "title",
                "description",
                "poster_url",
                "genres",
                "created_at",
                "updated_at",
            }

    # ``limit`` and ``offset`` slice that same, stably ordered list.
    total = whole["total"]
    for offset in range(min(total, 4)):
        page = _json_body(
            await client.get(_MOVIES_URL, params={"limit": 1, "offset": offset})
        )
        assert page["total"] == total
        assert page["limit"] == 1
        assert page["offset"] == offset
        assert [item["title"] for item in page["items"]] == [titles[offset]]

    # An offset past the end is an empty page, not an error.
    past_end = _json_body(
        await client.get(_MOVIES_URL, params={"limit": 2, "offset": 500})
    )
    assert past_end == {"items": [], "total": total, "limit": 2, "offset": 500}


async def test_list_movies_genre_filter_and_limit_offset_bounds_422(
    client: httpx.AsyncClient,
    admin_headers: _AdminHeaders,
    create_movie_via_api: _CreateMovie,
    rollback_connection: AsyncConnection,
) -> None:
    """Test 23 (FR-14): the genre filter narrows the page; bad bounds are 422."""
    headers = await admin_headers(
        client, rollback_connection, "spec3.movie.filter@example.com"
    )
    genre_a = await _create_genre(client, headers, "Spec3 Filter Genre A")
    genre_b = await _create_genre(client, headers, "Spec3 Filter Genre B")
    only_a = await create_movie_via_api(
        client, headers, "Spec3 Filter Movie A", [genre_a["id"]]
    )
    both = await create_movie_via_api(
        client, headers, "Spec3 Filter Movie Both", [genre_a["id"], genre_b["id"]]
    )
    only_b = await create_movie_via_api(
        client, headers, "Spec3 Filter Movie B", [genre_b["id"]]
    )

    page_a = _json_body(
        await client.get(_MOVIES_URL, params={"genre_id": genre_a["id"]})
    )
    assert page_a["total"] == 2
    assert {item["id"] for item in page_a["items"]} == {only_a["id"], both["id"]}
    for item in page_a["items"]:
        assert genre_a["id"] in {genre["id"] for genre in item["genres"]}

    page_b = _json_body(
        await client.get(_MOVIES_URL, params={"genre_id": genre_b["id"]})
    )
    assert page_b["total"] == 2
    assert {item["id"] for item in page_b["items"]} == {both["id"], only_b["id"]}

    # The filter and the window compose: 2 matches, second one alone.
    sliced = _json_body(
        await client.get(
            _MOVIES_URL, params={"genre_id": genre_a["id"], "limit": 1, "offset": 1}
        )
    )
    assert sliced["total"] == 2
    assert sliced["limit"] == 1
    assert sliced["offset"] == 1
    assert len(sliced["items"]) == 1

    # A genre nobody uses is an empty page, not a 404.
    unused_response = await client.get(_MOVIES_URL, params={"genre_id": _UNKNOWN_ID})
    assert unused_response.status_code == 200, unused_response.text
    unused = _json_body(unused_response)
    assert unused["items"] == []
    assert unused["total"] == 0

    # Out-of-range bounds are rejected, never clamped (FR-14).
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        _assert_error(
            await client.get(_MOVIES_URL, params=params), 422, "VALIDATION_ERROR"
        )


async def test_get_movie_unknown_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """Test 24 (FR-5): reading a movie that does not exist is a 404."""
    _assert_error(
        await client.get(f"{_MOVIES_URL}/{_UNKNOWN_ID}"), 404, "MOVIE_NOT_FOUND"
    )
