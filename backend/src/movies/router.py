"""Thin HTTP handlers for the genre and movie endpoints (SPEC-3 Section 2.3).

Every handler only parses its inputs, calls the matching function in
:mod:`src.movies.service` and shapes the response; the rules themselves live in
the service layer, so the route layer stays thin and the rules stay callable
without a framework or a live request (``docs/steering/conventions.md`` and
``principles.md``: "Business rules never live inside a FastAPI route handler").

Two routers keep the public catalogue reads apart from the admin writes, so the
generated documentation groups them and the admin ones are visibly the ones
behind :func:`~src.auth.dependencies.require_admin` (SPEC-3 Section 2.6):

    GET    /genres                    public_router, no token (FR-1)
    GET    /movies                    public_router, paginated (FR-14)
    GET    /movies/{movie_id}         public_router
    POST   /admin/genres              admin_router, 201
    DELETE /admin/genres/{genre_id}   admin_router, 204
    POST   /admin/movies              admin_router, 201
    PATCH  /admin/movies/{movie_id}   admin_router, 200
    DELETE /admin/movies/{movie_id}   admin_router, 204

``response_model`` is declared on every route, so a response is serialised from
the declared schema and no field the SPEC does not list can reach a client
(SPEC-3 Section 2.3). Failures are raised by the service as
:class:`~src.core.errors.ApiError` and rendered by the handlers installed in
``main.py``, so the error body shape stays defined in exactly one place
(FR-16). No handler here builds an error response itself.

Connections are requested through :data:`~src.db.engine.DbConnection`, the one
alias that carries ``scope="function"``, so a write is committed before the
response is sent and the handler shares one connection and one transaction with
the authorization dependency (SPEC-2 Sections 2.5, 2.7).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from src.auth.dependencies import require_admin
from src.auth.service import UserRecord
from src.core.pagination import Page, PageParams, get_page_params
from src.db.engine import DbConnection
from src.movies import service
from src.movies.schemas import (
    GenreCreate,
    GenreOut,
    MovieCreate,
    MovieOut,
    MovieUpdate,
)
from src.movies.service import GenreRecord, MovieRecord

# The public catalogue reads carry no prefix and no token; the admin writes sit
# under ``/admin``, exactly as the endpoint table of SPEC-3 Section 2.3 shows.
public_router = APIRouter(tags=["movies"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])


@public_router.get("/genres", response_model=list[GenreOut])
async def read_genres(conn: DbConnection) -> list[GenreRecord]:
    """Return every genre, ordered by name (SPEC-3 Section 2.3, FR-1).

    Args:
        conn: The request-scoped connection; this route only reads.

    Returns:
        list[GenreRecord]: All genres, sorted by lowercased name then id, each
        serialised through ``GenreOut``.

    Non-obvious: the route is public (FR-1) and the genre table is small enough
    that the SPEC gives it no pagination, so it accepts no query parameter at
    all.
    """
    return await service.list_genres(conn)


@public_router.get("/movies", response_model=Page[MovieOut])
async def read_movies(
    conn: DbConnection,
    page: Annotated[PageParams, Depends(get_page_params)],
    genre_id: int | None = None,
) -> Page[MovieOut]:
    """Return a page of movies with their genres (SPEC-3 Section 2.5, FR-14).

    Args:
        conn: The request-scoped connection; this route only reads.
        page: The validated ``limit`` / ``offset`` pair produced by the shared
            dependency. An out-of-range value was already rejected with the
            standard 422 ``VALIDATION_ERROR`` body and is never clamped (FR-14).
        genre_id: When given, only movies linked to this genre are returned. An
            unknown genre id is not an error -- the page is simply empty
            (SPEC-3 Section 2.5, "Browse movies"; decision D6 covers only ids
            referenced in a body).

    Returns:
        Page[MovieOut]: The page's movies, each with its genres sorted by name,
        plus the total number of matching movies (envelope per Section 2.3).
    """
    items, total = await service.list_movies(conn, page.limit, page.offset, genre_id)
    return Page[MovieOut](
        items=[MovieOut.model_validate(item, from_attributes=True) for item in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@public_router.get("/movies/{movie_id}", response_model=MovieOut)
async def read_movie(movie_id: int, conn: DbConnection) -> MovieRecord:
    """Return one movie with its genres (SPEC-3 Section 2.3, FR-5).

    Args:
        movie_id: The movie's primary key, taken from the path.
        conn: The request-scoped connection; this route only reads.

    Returns:
        MovieRecord: The movie, with its genres sorted by name, serialised
        through ``MovieOut``.

    Raises:
        ApiError: ``MOVIE_NOT_FOUND`` with status 404 when no movie has that id
            (FR-5). The service raises it; ``main.py`` renders it.
    """
    return await service.get_movie(conn, movie_id)


@admin_router.post(
    "/genres",
    response_model=GenreOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_genre(
    payload: GenreCreate,
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
) -> GenreRecord:
    """Create a genre and return it (SPEC-3 Section 2.3, FR-8).

    Args:
        payload: The validated request body. ``name`` is already stripped and
            bounded, and extra fields were already rejected with 422 (FR-4).
        admin: The acting administrator, resolved by ``require_admin``. Its id
            is passed to the service for the audit log line only and is never
            written to the row (SPEC-3 Section 7).
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        GenreRecord: The created genre, serialised through ``GenreOut``.

    Raises:
        ApiError: ``GENRE_ALREADY_EXISTS`` with status 409 when the name is
            already taken ignoring case (FR-8); ``UNAUTHENTICATED`` with status
            401 for an anonymous caller or ``FORBIDDEN`` with status 403 for an
            authenticated non-admin (FR-2), both from the dependency.
    """
    return await service.create_genre(conn, payload.name, admin.id)


@admin_router.delete("/genres/{genre_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_genre(
    genre_id: int,
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
) -> None:
    """Delete a genre that no movie uses (SPEC-3 Section 2.3, FR-8).

    Args:
        genre_id: The genre's primary key, taken from the path.
        admin: The acting administrator, resolved by ``require_admin``; its id
            is passed to the service for the audit log line only.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        None: The route answers 204 with an empty body, as Section 2.3
        requires.

    Raises:
        ApiError: ``GENRE_NOT_FOUND`` with status 404 when no genre has that id,
            or ``GENRE_IN_USE`` with status 409 when at least one movie still
            references it (FR-8); plus 401/403 from the dependency.
    """
    await service.delete_genre(conn, genre_id, admin.id)


@admin_router.post(
    "/movies",
    response_model=MovieOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_movie(
    payload: MovieCreate,
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
) -> MovieRecord:
    """Create a movie together with its genre links (SPEC-3 FR-3).

    Args:
        payload: The validated request body. Its genre ids are already distinct
            and within the count limit, and extra fields were already rejected
            with 422 (FR-4).
        admin: The acting administrator, resolved by ``require_admin``; its id
            is used for the audit log line only.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        MovieRecord: The created movie, with its genres sorted by name,
        serialised through ``MovieOut``.

    Raises:
        ApiError: ``GENRE_NOT_FOUND`` with status 404 when a referenced genre
            id does not exist, in which case nothing is written (FR-5); plus
            401/403 from the dependency.
    """
    return await service.create_movie(conn, payload, admin.id)


@admin_router.patch("/movies/{movie_id}", response_model=MovieOut)
async def update_movie(
    movie_id: int,
    payload: MovieUpdate,
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
) -> MovieRecord:
    """Apply a partial update to a movie (SPEC-3 FR-6).

    Args:
        movie_id: The movie's primary key, taken from the path.
        payload: The validated request body. Only the fields actually present
            in the body are applied, and an empty body was already rejected
            with 422 (FR-4); the service reads ``model_fields_set`` to decide.
        admin: The acting administrator, resolved by ``require_admin``; its id
            is used for the audit log line only.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        MovieRecord: The updated movie, with its genres sorted by name,
        serialised through ``MovieOut``.

    Raises:
        ApiError: ``MOVIE_NOT_FOUND`` or ``GENRE_NOT_FOUND`` with status 404 --
            the first when no movie has that id, the second when a supplied
            ``genre_ids`` names a genre that does not exist (FR-5, FR-6); plus
            401/403 from the dependency.
    """
    return await service.update_movie(conn, movie_id, payload, admin.id)


@admin_router.delete("/movies/{movie_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_movie(
    movie_id: int,
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
) -> None:
    """Delete a movie that has no showtimes (SPEC-3 FR-7).

    Args:
        movie_id: The movie's primary key, taken from the path.
        admin: The acting administrator, resolved by ``require_admin``; its id
            is used for the audit log line only.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        None: The route answers 204 with an empty body. The movie's
        ``movie_genres`` links go with it through the existing
        ``ON DELETE CASCADE``; they are not deleted here.

    Raises:
        ApiError: ``MOVIE_NOT_FOUND`` with status 404 when no movie has that id,
            or ``MOVIE_HAS_SHOWTIMES`` with status 409 when the movie still has
            at least one showtime, past or future (FR-7, decision D3); plus
            401/403 from the dependency.
    """
    await service.delete_movie(conn, movie_id, admin.id)
