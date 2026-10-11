"""Genre and movie business rules over an open database connection.

This module owns the genre and movie rules from SPEC-3 Sections 2.5 and 2.7
(FR-3 through FR-8, FR-14 for the movie list, FR-15). It is deliberately free of
HTTP and FastAPI imports: every function takes an already-open
:class:`~sqlalchemy.ext.asyncio.AsyncConnection`, never opens one itself and
never commits, so the request-scoped transaction stays owned by
:func:`src.db.engine.get_connection` and the same functions can be driven
straight from a test connection.

Import direction (SPEC-3 Section 11.1): ``movies`` may import ``core``, ``db``
and ``auth.dependencies``. It does **not** import ``showtimes`` -- the movie
delete pre-check reads the ``showtimes`` table through :mod:`src.db.tables`
directly. Within the module, ``service`` imports :mod:`src.movies.schemas` for
the validated request models and :mod:`src.movies.errors` for the error
builders; neither imports ``service``, so there is no cycle.

Query shape notes:

* ``list_movies`` loads the genres for a whole page in ONE additional query
  (``WHERE movie_id IN (...)``) rather than one query per row, so there is no
  N+1 (SPEC-3 Section 2.5, "Browse movies").
* Every returned value is a small frozen dataclass, never a live SQLAlchemy
  :class:`~sqlalchemy.engine.Row`, so a row cannot be read after its transaction
  has ended.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Row, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from src.db.tables import genres, movie_genres, movies, showtimes
from src.movies.errors import (
    genre_already_exists,
    genre_in_use,
    genre_not_found,
    movie_has_showtimes,
    movie_not_found,
)
from src.movies.schemas import MovieCreate, MovieUpdate

logger = logging.getLogger(__name__)

# The ``movies`` columns every read and the insert/update ``RETURNING`` clause
# share, so the selected shape cannot drift from the one ``MovieRecord`` maps.
_MOVIE_COLUMNS = (
    movies.c.id,
    movies.c.title,
    movies.c.description,
    movies.c.poster_url,
    movies.c.created_at,
    movies.c.updated_at,
)


@dataclass(frozen=True, slots=True)
class GenreRecord:
    """A genre as the service returns it.

    Attributes:
        id: The genre's primary key.
        name: The stored name.
    """

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class MovieRecord:
    """A movie with its genres, as the service returns it.

    The public shape mirrors SPEC-3's ``MovieOut`` so the route layer can hand
    this straight to FastAPI's response model.

    Attributes:
        id: The movie's primary key.
        title: The stored title.
        description: The stored description (never ``None``).
        poster_url: The stored poster URL, or ``None`` when there is no poster.
        genres: The linked genres, sorted by lowercased name then id.
        created_at: Row creation time, in UTC.
        updated_at: Time of the last change, in UTC.
    """

    id: int
    title: str
    description: str
    poster_url: str | None
    genres: tuple[GenreRecord, ...]
    created_at: datetime
    updated_at: datetime


def _movie_record(
    row: Row[*tuple[Any, ...]],
    movie_genres_records: Sequence[GenreRecord],
) -> MovieRecord:
    """Map a selected ``movies`` row plus its genres to a :class:`MovieRecord`.

    Args:
        row: A row holding the columns in :data:`_MOVIE_COLUMNS`.
        movie_genres_records: The movie's genres, already sorted by name.

    Returns:
        MovieRecord: The row as the public shape.
    """
    return MovieRecord(
        id=row.id,
        title=row.title,
        description=row.description,
        poster_url=row.poster_url,
        genres=tuple(movie_genres_records),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _genres_by_movie(
    conn: AsyncConnection,
    movie_ids: Sequence[int],
) -> dict[int, tuple[GenreRecord, ...]]:
    """Load the genres of several movies in one query (no N+1).

    Args:
        conn: An open connection. This function only reads.
        movie_ids: The ids of the page's movies. An empty sequence returns an
            empty mapping without touching the database.

    Returns:
        dict[int, tuple[GenreRecord, ...]]: Movie id to its genres, each sorted
        by lowercased name then id. A movie with no genres is simply absent.
    """
    if not movie_ids:
        return {}

    rows = (
        await conn.execute(
            select(movie_genres.c.movie_id, genres.c.id, genres.c.name)
            .join(genres, genres.c.id == movie_genres.c.genre_id)
            .where(movie_genres.c.movie_id.in_(movie_ids))
            .order_by(func.lower(genres.c.name), genres.c.id)
        )
    ).all()

    collected: dict[int, list[GenreRecord]] = {}
    for row in rows:
        collected.setdefault(row.movie_id, []).append(
            GenreRecord(id=row.id, name=row.name)
        )
    return {movie_id: tuple(records) for movie_id, records in collected.items()}


async def _load_genres_or_404(
    conn: AsyncConnection,
    genre_ids: Sequence[int],
) -> tuple[GenreRecord, ...]:
    """Load every referenced genre or fail with 404 (SPEC-3 FR-5, decision D6).

    A referenced id that does not exist is answered with the matching
    ``GENRE_NOT_FOUND`` rather than a 422, and the check is one query for the
    whole set (SPEC-3 Section 2.5, "Movie create/update").

    Args:
        conn: An open connection. This function only reads.
        genre_ids: The distinct ids to load; an empty sequence returns an empty
            tuple.

    Returns:
        tuple[GenreRecord, ...]: The genres, sorted by lowercased name then id.

    Raises:
        ApiError: ``GENRE_NOT_FOUND`` with status 404 when at least one id has
            no matching row.
    """
    if not genre_ids:
        return ()

    rows = (
        await conn.execute(
            select(genres.c.id, genres.c.name)
            .where(genres.c.id.in_(genre_ids))
            .order_by(func.lower(genres.c.name), genres.c.id)
        )
    ).all()

    # ``genre_ids`` is validated as distinct, but ``set`` keeps this correct even
    # for a direct service call.
    if len(rows) != len(set(genre_ids)):
        raise genre_not_found()
    return tuple(GenreRecord(id=row.id, name=row.name) for row in rows)


async def list_genres(conn: AsyncConnection) -> list[GenreRecord]:
    """List every genre, ordered by name (SPEC-3 Section 2.3, FR-1).

    Ordering is by the lowercased name and then by id, so the list reads the
    same regardless of the case a name was stored with and ties are
    deterministic.

    Args:
        conn: An open connection. This function only reads.

    Returns:
        list[GenreRecord]: All genres, sorted.
    """
    rows = (
        await conn.execute(
            select(genres.c.id, genres.c.name).order_by(
                func.lower(genres.c.name), genres.c.id
            )
        )
    ).all()
    return [GenreRecord(id=row.id, name=row.name) for row in rows]


async def create_genre(
    conn: AsyncConnection,
    name: str,
    actor_id: int,
) -> GenreRecord:
    """Create a genre, refusing a case-insensitive duplicate (SPEC-3 FR-8).

    Duplicate detection is layered, following the ``signup`` pattern reused by
    SPEC-3 Section 8:

    * a case-insensitive ``lower(name) = lower(:name)`` pre-check gives a clean
      409 for the common case, and
    * the insert itself runs inside a SAVEPOINT (``begin_nested``) so an
      ``IntegrityError`` from the UNIQUE constraint -- a concurrent request that
      slipped past the pre-check -- is mapped to the same 409 without poisoning
      the surrounding request transaction.

    Two simultaneous requests that differ only by letter case can both pass the
    case-*sensitive* UNIQUE constraint; SPEC-3 Section 2.5 accepts that as
    cosmetic.

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        name: The new name, already stripped and bounded by ``GenreCreate``.
        actor_id: The id of the signed-in admin performing the write. Used only
            for the audit log line and never written to the row.

    Returns:
        GenreRecord: The created genre.

    Raises:
        ApiError: ``GENRE_ALREADY_EXISTS`` with status 409 when the name already
            exists ignoring case.
    """
    existing = (
        await conn.execute(
            select(genres.c.id)
            .where(func.lower(genres.c.name) == name.lower())
            .limit(1)
        )
    ).one_or_none()
    if existing is not None:
        logger.info("genre_create_rejected reason=already_exists")
        raise genre_already_exists()

    try:
        async with conn.begin_nested():
            row = (
                await conn.execute(
                    insert(genres)
                    .values(name=name)
                    .returning(genres.c.id, genres.c.name)
                )
            ).one()
    except IntegrityError as error:
        # The only uniqueness rule on ``genres`` is the name, so any integrity
        # failure here is a duplicate name (raised as race the pre-check missed).
        logger.info("genre_create_rejected reason=duplicate_name")
        raise genre_already_exists() from error

    logger.info("genre_created actor=%s genre_id=%s", actor_id, row.id)
    return GenreRecord(id=row.id, name=row.name)


async def delete_genre(
    conn: AsyncConnection,
    genre_id: int,
    actor_id: int,
) -> None:
    """Delete a genre that no movie uses (SPEC-3 FR-8).

    ``movie_genres.genre_id`` uses ``ON DELETE RESTRICT``, so a genre still
    attached to a movie cannot be removed; the pre-check turns that into a clean
    409 instead of an ``IntegrityError``.

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        genre_id: The genre's primary key.
        actor_id: The id of the signed-in admin performing the write. Used only
            for the audit log line.

    Returns:
        None.

    Raises:
        ApiError: ``GENRE_NOT_FOUND`` with status 404 when no genre has that id,
            or ``GENRE_IN_USE`` with status 409 when at least one
            ``movie_genres`` row references it.
    """
    exists = (
        await conn.execute(select(genres.c.id).where(genres.c.id == genre_id))
    ).one_or_none()
    if exists is None:
        raise genre_not_found()

    in_use = (
        await conn.execute(
            select(movie_genres.c.movie_id)
            .where(movie_genres.c.genre_id == genre_id)
            .limit(1)
        )
    ).one_or_none()
    if in_use is not None:
        logger.info("genre_delete_rejected reason=in_use genre_id=%s", genre_id)
        raise genre_in_use()

    await conn.execute(delete(genres).where(genres.c.id == genre_id))
    logger.info("genre_deleted actor=%s genre_id=%s", actor_id, genre_id)


async def list_movies(
    conn: AsyncConnection,
    limit: int,
    offset: int,
    genre_id: int | None,
) -> tuple[list[MovieRecord], int]:
    """List a page of movies with their genres (SPEC-3 Section 2.5, FR-14).

    Order is by lowercased title then id. ``total`` counts every matching movie,
    ignoring ``limit``/``offset``, so a client can compute the number of pages.
    An optional ``genre_id`` filters the list; an unknown genre id is not an
    error -- it simply matches nothing and yields an empty page (SPEC-3 Section
    2.5, "Browse movies"). The genres of the whole page are loaded in one extra
    query, so there is no N+1.

    Args:
        conn: An open connection. This function only reads.
        limit: Maximum number of movies to return (already validated 1..100).
        offset: Number of movies to skip (already validated >= 0).
        genre_id: When given, only movies linked to this genre are returned.

    Returns:
        tuple[list[MovieRecord], int]: The page's movies (each with its genres
        sorted by name) and the total number of matching movies.
    """
    page_statement = select(*_MOVIE_COLUMNS)
    count_statement = select(func.count()).select_from(movies)
    if genre_id is not None:
        page_statement = page_statement.join(
            movie_genres, movie_genres.c.movie_id == movies.c.id
        ).where(movie_genres.c.genre_id == genre_id)
        count_statement = (
            select(func.count())
            .select_from(
                movies.join(movie_genres, movie_genres.c.movie_id == movies.c.id)
            )
            .where(movie_genres.c.genre_id == genre_id)
        )

    rows = (
        await conn.execute(
            page_statement.order_by(func.lower(movies.c.title), movies.c.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = (await conn.execute(count_statement)).scalar_one()

    genre_map = await _genres_by_movie(conn, [row.id for row in rows])
    items = [_movie_record(row, genre_map.get(row.id, ())) for row in rows]
    return items, total


async def get_movie(conn: AsyncConnection, movie_id: int) -> MovieRecord:
    """Load one movie with its genres (SPEC-3 Section 2.3).

    Args:
        conn: An open connection. This function only reads.
        movie_id: The movie's primary key.

    Returns:
        MovieRecord: The movie, with its genres sorted by name.

    Raises:
        ApiError: ``MOVIE_NOT_FOUND`` with status 404 when no movie has that id.
    """
    row = (
        await conn.execute(select(*_MOVIE_COLUMNS).where(movies.c.id == movie_id))
    ).one_or_none()
    if row is None:
        raise movie_not_found()

    genre_map = await _genres_by_movie(conn, [movie_id])
    return _movie_record(row, genre_map.get(movie_id, ()))


async def create_movie(
    conn: AsyncConnection,
    data: MovieCreate,
    actor_id: int,
) -> MovieRecord:
    """Create a movie together with its genre links (SPEC-3 FR-3).

    Every referenced genre is checked with one query first, so an unknown id is
    answered with 404 ``GENRE_NOT_FOUND`` and nothing is written (FR-5). The
    movie row and all ``movie_genres`` links are inserted through the caller's
    transaction, so either the movie with its links exists or neither does.
    ``created_at`` and ``updated_at`` come from the table's server defaults.

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        data: The validated request body; its genre ids are already distinct.
        actor_id: The id of the signed-in admin performing the write. Used only
            for the audit log line.

    Returns:
        MovieRecord: The created movie, with its genres sorted by name.

    Raises:
        ApiError: ``GENRE_NOT_FOUND`` with status 404 when any referenced genre
            id does not exist.
    """
    genre_records = await _load_genres_or_404(conn, data.genre_ids)

    row = (
        await conn.execute(
            insert(movies)
            .values(
                title=data.title,
                description=data.description,
                poster_url=data.poster_url,
            )
            .returning(*_MOVIE_COLUMNS)
        )
    ).one()

    if genre_records:
        await conn.execute(
            insert(movie_genres),
            [{"movie_id": row.id, "genre_id": genre.id} for genre in genre_records],
        )

    logger.info("movie_created actor=%s movie_id=%s", actor_id, row.id)
    return _movie_record(row, genre_records)


async def update_movie(
    conn: AsyncConnection,
    movie_id: int,
    data: MovieUpdate,
    actor_id: int,
) -> MovieRecord:
    """Apply a partial update to a movie (SPEC-3 FR-6).

    Only the fields actually supplied in the request are written -- the set is
    read from ``data.model_fields_set``. ``genre_ids`` is special: when supplied
    it replaces the whole link set (delete the old rows, insert the new ones),
    and when absent the links are left untouched. ``updated_at`` is always set to
    the current UTC time here, because the SPEC-1 schema has no trigger for it,
    so even an update to identical values bumps it (SPEC-3 Section 3).

    The movie is updated first; a missing id is answered with 404 before any
    genre link is touched, and an unknown referenced genre (checked before the
    links are rewritten) is answered with 404. Either way ``get_connection``
    rolls the whole request transaction back, so a failed call changes nothing.

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        movie_id: The movie's primary key.
        data: The validated request body. Its ``model_fields_set`` decides which
            columns change and whether the genre set is replaced.
        actor_id: The id of the signed-in admin performing the write. Used only
            for the audit log line.

    Returns:
        MovieRecord: The updated movie, with its genres sorted by name.

    Raises:
        ApiError: ``MOVIE_NOT_FOUND`` with status 404 when no movie has that id,
            or ``GENRE_NOT_FOUND`` with status 404 when a supplied
            ``genre_ids`` references an id that does not exist.
    """
    supplied = data.model_fields_set
    values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
    if "title" in supplied:
        values["title"] = data.title
    if "description" in supplied:
        values["description"] = data.description
    if "poster_url" in supplied:
        values["poster_url"] = data.poster_url

    row = (
        await conn.execute(
            update(movies)
            .where(movies.c.id == movie_id)
            .values(**values)
            .returning(*_MOVIE_COLUMNS)
        )
    ).one_or_none()
    if row is None:
        raise movie_not_found()

    if "genre_ids" in supplied:
        genre_records = await _load_genres_or_404(conn, data.genre_ids or [])
        await conn.execute(
            delete(movie_genres).where(movie_genres.c.movie_id == movie_id)
        )
        if genre_records:
            await conn.execute(
                insert(movie_genres),
                [
                    {"movie_id": movie_id, "genre_id": genre.id}
                    for genre in genre_records
                ],
            )
    else:
        genre_map = await _genres_by_movie(conn, [movie_id])
        genre_records = genre_map.get(movie_id, ())

    logger.info("movie_updated actor=%s movie_id=%s", actor_id, movie_id)
    return _movie_record(row, genre_records)


async def delete_movie(
    conn: AsyncConnection,
    movie_id: int,
    actor_id: int,
) -> None:
    """Delete a movie that has no showtimes (SPEC-3 FR-7, decision D3).

    Deleting is refused when the movie has *any* showtime, past or future,
    because ``showtimes.movie_id`` uses ``ON DELETE RESTRICT`` and the stricter
    rule keeps the history of past showtimes intact. The pre-check turns the
    common case into a clean 409; the delete itself runs inside a SAVEPOINT so
    that a showtime inserted concurrently -- the case the pre-check cannot see --
    surfaces as the same 409 instead of poisoning the request transaction.

    ``movie_genres`` rows are removed by the existing ``ON DELETE CASCADE``, so
    they are not deleted explicitly.

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        movie_id: The movie's primary key.
        actor_id: The id of the signed-in admin performing the write. Used only
            for the audit log line.

    Returns:
        None.

    Raises:
        ApiError: ``MOVIE_NOT_FOUND`` with status 404 when no movie has that id,
            or ``MOVIE_HAS_SHOWTIMES`` with status 409 when the movie still has
            at least one showtime.
    """
    exists = (
        await conn.execute(select(movies.c.id).where(movies.c.id == movie_id))
    ).one_or_none()
    if exists is None:
        raise movie_not_found()

    has_showtimes = (
        await conn.execute(
            select(showtimes.c.id).where(showtimes.c.movie_id == movie_id).limit(1)
        )
    ).one_or_none()
    if has_showtimes is not None:
        logger.info("movie_delete_rejected reason=has_showtimes movie_id=%s", movie_id)
        raise movie_has_showtimes()

    try:
        async with conn.begin_nested():
            await conn.execute(delete(movies).where(movies.c.id == movie_id))
    except IntegrityError as error:
        logger.info("movie_delete_rejected reason=has_showtimes movie_id=%s", movie_id)
        raise movie_has_showtimes() from error

    logger.info("movie_deleted actor=%s movie_id=%s", actor_id, movie_id)
