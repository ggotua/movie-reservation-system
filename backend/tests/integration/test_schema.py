"""Integration tests that prove SPEC-1's database constraints are enforced.

These tests run against a real PostgreSQL server (no mocking) because the point
is to prove the DATABASE-level constraints and indexes defined in
``src/db/tables.py`` actually reject bad data — behaviour a mock cannot
demonstrate.

Database target: the URL in the ``TEST_DATABASE_URL`` environment variable,
read the same way ``src/db/seed.py`` reads its settings (``pydantic-settings``,
loading ``backend/.env`` then real environment variables; fails loudly if
unset). No Docker and no ``testcontainers`` are used — the developer's machine
cannot run virtualization, and CI uses a GitHub Actions PostgreSQL service
container.

Engine style: a synchronous ``create_engine("postgresql+psycopg://...")`` and
plain synchronous pytest tests/fixtures. Async psycopg fails on Windows under
the default ``ProactorEventLoop``, and these tests only prove database
constraints, so async would add cost without adding coverage. The shared
fixtures live in ``tests/conftest.py``.

Every test name matches SPEC-1 Section 5 exactly; each test that verifies a
numbered requirement carries a comment naming the FR it covers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, insert, inspect, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import Executable

from src.db.tables import (
    genres,
    movie_genres,
    movies,
    reservations,
    screens,
    seat_reservations,
    seats,
    showtimes,
    users,
)


def _default_starts_at() -> datetime:
    """Return a future showtime timestamp in UTC.

    Returns:
        datetime: ``now(UTC)`` plus one day, used as the default ``starts_at``
        so fixtures never depend on the current time.
    """
    return datetime.now(UTC) + timedelta(days=1)


def _expect_integrity_error(connection: Connection, statement: Executable) -> None:
    """Execute ``statement`` and assert the database rejects it.

    The statement runs inside a SAVEPOINT so a rejected write rolls back only
    that savepoint; the surrounding test transaction stays usable for later
    statements (PostgreSQL aborts an entire transaction after any error).

    Args:
        connection: The open test connection.
        statement: The INSERT/DELETE expected to violate a constraint.

    Raises:
        AssertionError: If no ``IntegrityError`` is raised.
    """
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(statement)


def _insert_user(
    connection: Connection,
    *,
    email: str = "user@example.com",
    password_hash: str = "not-a-real-hash",
    role: str = "user",
) -> int:
    """Insert a ``users`` row and return its id.

    Args:
        connection: The open test connection.
        email: Unique email address for the row.
        password_hash: Stored hash; the literal is a placeholder, never a real
            password.
        role: One of the ``users_role_check`` values (``user``/``admin``).

    Returns:
        int: The new row's ``id``.
    """
    statement = (
        insert(users)
        .values(email=email, password_hash=password_hash, role=role)
        .returning(users.c.id)
    )
    return int(connection.execute(statement).scalar_one())


def _insert_movie(connection: Connection, *, title: str = "Test Movie") -> int:
    """Insert a ``movies`` row and return its id.

    Args:
        connection: The open test connection.
        title: Movie title.

    Returns:
        int: The new row's ``id``.
    """
    statement = insert(movies).values(title=title).returning(movies.c.id)
    return int(connection.execute(statement).scalar_one())


def _insert_genre(connection: Connection, *, name: str = "Action") -> int:
    """Insert a ``genres`` row and return its id.

    Args:
        connection: The open test connection.
        name: Unique genre name.

    Returns:
        int: The new row's ``id``.
    """
    statement = insert(genres).values(name=name).returning(genres.c.id)
    return int(connection.execute(statement).scalar_one())


def _insert_screen(
    connection: Connection,
    *,
    name: str = "Screen 1",
    rows: int = 8,
    seats_per_row: int = 10,
) -> int:
    """Insert a ``screens`` row and return its id.

    Args:
        connection: The open test connection.
        name: Unique screen name.
        rows: Number of seat rows (must be positive).
        seats_per_row: Seats per row (must be positive).

    Returns:
        int: The new row's ``id``.
    """
    statement = (
        insert(screens)
        .values(name=name, rows=rows, seats_per_row=seats_per_row)
        .returning(screens.c.id)
    )
    return int(connection.execute(statement).scalar_one())


def _insert_seat(
    connection: Connection,
    *,
    screen_id: int,
    row_label: str = "A",
    seat_number: int = 1,
) -> int:
    """Insert a ``seats`` row and return its id.

    Args:
        connection: The open test connection.
        screen_id: Parent screen id.
        row_label: Row letter, e.g. ``"A"``.
        seat_number: Seat number within the row.

    Returns:
        int: The new row's ``id``.
    """
    statement = (
        insert(seats)
        .values(screen_id=screen_id, row_label=row_label, seat_number=seat_number)
        .returning(seats.c.id)
    )
    return int(connection.execute(statement).scalar_one())


def _insert_showtime(
    connection: Connection,
    *,
    movie_id: int,
    screen_id: int,
    starts_at: datetime | None = None,
    price_cents: int = 1200,
) -> int:
    """Insert a ``showtimes`` row and return its id.

    Args:
        connection: The open test connection.
        movie_id: Parent movie id.
        screen_id: Parent screen id.
        starts_at: Showtime timestamp; defaults to :func:`_default_starts_at`.
        price_cents: Ticket price in integer cents (must be positive).

    Returns:
        int: The new row's ``id``.
    """
    statement = (
        insert(showtimes)
        .values(
            movie_id=movie_id,
            screen_id=screen_id,
            starts_at=starts_at if starts_at is not None else _default_starts_at(),
            price_cents=price_cents,
        )
        .returning(showtimes.c.id)
    )
    return int(connection.execute(statement).scalar_one())


def _insert_reservation(
    connection: Connection,
    *,
    user_id: int,
    showtime_id: int,
    status: str = "held",
) -> int:
    """Insert a ``reservations`` row and return its id.

    Args:
        connection: The open test connection.
        user_id: Parent user id.
        showtime_id: Parent showtime id.
        status: Reservation status (defaults to ``held``).

    Returns:
        int: The new row's ``id``.
    """
    statement = (
        insert(reservations)
        .values(user_id=user_id, showtime_id=showtime_id, status=status)
        .returning(reservations.c.id)
    )
    return int(connection.execute(statement).scalar_one())


def _insert_seat_reservation(
    connection: Connection,
    *,
    reservation_id: int,
    showtime_id: int,
    seat_id: int,
    status: str = "held",
) -> int:
    """Insert a ``seat_reservations`` row and return its id.

    Args:
        connection: The open test connection.
        reservation_id: Parent reservation id.
        showtime_id: Showtime id; must match the reservation's showtime (FR-7).
        seat_id: Reserved seat id.
        status: Seat-reservation status (defaults to ``held``).

    Returns:
        int: The new row's ``id``.
    """
    statement = (
        insert(seat_reservations)
        .values(
            reservation_id=reservation_id,
            showtime_id=showtime_id,
            seat_id=seat_id,
            status=status,
        )
        .returning(seat_reservations.c.id)
    )
    return int(connection.execute(statement).scalar_one())


def _create_screen_seat_showtime(connection: Connection) -> tuple[int, int]:
    """Create one movie, screen, seat and showtime in the same screen.

    Args:
        connection: The open test connection.

    Returns:
        tuple[int, int]: ``(seat_id, showtime_id)`` for the created seat and
        showtime, ready to be referenced by a reservation.
    """
    movie_id = _insert_movie(connection)
    screen_id = _insert_screen(connection)
    seat_id = _insert_seat(connection, screen_id=screen_id)
    showtime_id = _insert_showtime(connection, movie_id=movie_id, screen_id=screen_id)
    return seat_id, showtime_id


# --- SPEC-1 Section 5, tests 1-13 -----------------------------------------


def test_create_user_and_unique_email_case_insensitive(
    db_connection: Connection,
) -> None:
    """Verifies FR-1: email uniqueness is case-insensitive."""
    _insert_user(db_connection, email="Alice@Example.com")
    _expect_integrity_error(
        db_connection,
        insert(users).values(email="alice@example.com", password_hash="hash"),
    )


def test_users_role_check_rejects_invalid_role(db_connection: Connection) -> None:
    """Verifies the ``users_role_check`` constraint (SPEC-1 2.1)."""
    _expect_integrity_error(
        db_connection,
        insert(users).values(
            email="admin@example.com", password_hash="hash", role="superuser"
        ),
    )


def test_movie_genre_many_to_many(db_connection: Connection) -> None:
    """Verifies the ``movie_genres`` join (SPEC-1 2.4) links many genres."""
    movie_id = _insert_movie(db_connection)
    action_id = _insert_genre(db_connection, name="Action")
    comedy_id = _insert_genre(db_connection, name="Comedy")
    db_connection.execute(
        insert(movie_genres),
        [
            {"movie_id": movie_id, "genre_id": action_id},
            {"movie_id": movie_id, "genre_id": comedy_id},
        ],
    )
    linked = db_connection.execute(
        select(func.count())
        .select_from(movie_genres)
        .where(movie_genres.c.movie_id == movie_id)
    ).scalar_one()
    assert linked == 2


def test_delete_genre_in_use_is_restricted(db_connection: Connection) -> None:
    """Verifies ``genres`` -> ``movie_genres`` ON DELETE RESTRICT (SPEC-1 2.4)."""
    movie_id = _insert_movie(db_connection)
    genre_id = _insert_genre(db_connection, name="Drama")
    db_connection.execute(
        insert(movie_genres).values(movie_id=movie_id, genre_id=genre_id)
    )
    _expect_integrity_error(
        db_connection, delete(genres).where(genres.c.id == genre_id)
    )


def test_delete_movie_with_showtimes_is_restricted(
    db_connection: Connection,
) -> None:
    """Verifies FR-4: a movie with showtimes cannot be deleted (RESTRICT)."""
    movie_id = _insert_movie(db_connection)
    screen_id = _insert_screen(db_connection)
    _insert_showtime(db_connection, movie_id=movie_id, screen_id=screen_id)
    _expect_integrity_error(
        db_connection, delete(movies).where(movies.c.id == movie_id)
    )


def test_delete_screen_with_showtimes_is_restricted(
    db_connection: Connection,
) -> None:
    """Verifies FR-4: a screen with showtimes cannot be deleted (RESTRICT)."""
    movie_id = _insert_movie(db_connection)
    screen_id = _insert_screen(db_connection)
    _insert_showtime(db_connection, movie_id=movie_id, screen_id=screen_id)
    _expect_integrity_error(
        db_connection, delete(screens).where(screens.c.id == screen_id)
    )


def test_seat_unique_position_per_screen(db_connection: Connection) -> None:
    """Verifies ``seats_unique_position`` is scoped to one screen (SPEC-1 2.6)."""
    screen_a = _insert_screen(db_connection, name="Screen A")
    screen_b = _insert_screen(db_connection, name="Screen B")
    _insert_seat(db_connection, screen_id=screen_a, row_label="A", seat_number=1)
    _expect_integrity_error(
        db_connection,
        insert(seats).values(screen_id=screen_a, row_label="A", seat_number=1),
    )
    # The same (row, number) on a different screen is allowed.
    _insert_seat(db_connection, screen_id=screen_b, row_label="A", seat_number=1)


def test_showtime_price_must_be_positive(db_connection: Connection) -> None:
    """Verifies FR-5: ``price_cents`` must be > 0 (boundary -1, 0, 1)."""
    movie_id = _insert_movie(db_connection)
    screen_id = _insert_screen(db_connection)
    _insert_showtime(
        db_connection, movie_id=movie_id, screen_id=screen_id, price_cents=1
    )
    _expect_integrity_error(
        db_connection,
        insert(showtimes).values(
            movie_id=movie_id,
            screen_id=screen_id,
            starts_at=_default_starts_at(),
            price_cents=0,
        ),
    )
    _expect_integrity_error(
        db_connection,
        insert(showtimes).values(
            movie_id=movie_id,
            screen_id=screen_id,
            starts_at=_default_starts_at(),
            price_cents=-1,
        ),
    )


def test_seat_reservation_unique_active_constraint(
    db_connection: Connection,
) -> None:
    """Acceptance test for SPEC-1. Verifies FR-2.

    Two ACTIVE rows for the same ``(showtime_id, seat_id)`` cannot coexist: the
    partial unique index ``idx_seat_reservations_active_unique`` rejects the
    second one. Without that partial index both inserts would succeed, so this
    test fails against a schema that lacks it.
    """
    seat_id, showtime_id = _create_screen_seat_showtime(db_connection)
    user_id = _insert_user(db_connection)
    first_reservation = _insert_reservation(
        db_connection, user_id=user_id, showtime_id=showtime_id
    )
    second_reservation = _insert_reservation(
        db_connection, user_id=user_id, showtime_id=showtime_id
    )
    _insert_seat_reservation(
        db_connection,
        reservation_id=first_reservation,
        showtime_id=showtime_id,
        seat_id=seat_id,
        status="held",
    )
    _expect_integrity_error(
        db_connection,
        insert(seat_reservations).values(
            reservation_id=second_reservation,
            showtime_id=showtime_id,
            seat_id=seat_id,
            status="confirmed",
        ),
    )


def test_seat_reservation_allows_new_hold_after_prior_cancelled(
    db_connection: Connection,
) -> None:
    """Acceptance test for SPEC-1. Verifies FR-3.

    A terminal (``cancelled``) row is excluded from the partial index
    ``WHERE status IN ('held', 'confirmed')``, so a new active hold for the same
    seat/showtime is allowed. Together with test 9 this pins both sides of the
    active/terminal boundary.
    """
    seat_id, showtime_id = _create_screen_seat_showtime(db_connection)
    user_id = _insert_user(db_connection)
    cancelled_reservation = _insert_reservation(
        db_connection, user_id=user_id, showtime_id=showtime_id
    )
    new_reservation = _insert_reservation(
        db_connection, user_id=user_id, showtime_id=showtime_id
    )
    _insert_seat_reservation(
        db_connection,
        reservation_id=cancelled_reservation,
        showtime_id=showtime_id,
        seat_id=seat_id,
        status="cancelled",
    )
    new_hold_id = _insert_seat_reservation(
        db_connection,
        reservation_id=new_reservation,
        showtime_id=showtime_id,
        seat_id=seat_id,
        status="held",
    )
    assert new_hold_id > 0


def test_reservation_cascade_deletes_seat_reservations(
    db_connection: Connection,
) -> None:
    """Verifies FR-6: deleting a reservation cascades to its seat_reservations."""
    seat_id, showtime_id = _create_screen_seat_showtime(db_connection)
    user_id = _insert_user(db_connection)
    reservation_id = _insert_reservation(
        db_connection, user_id=user_id, showtime_id=showtime_id
    )
    _insert_seat_reservation(
        db_connection,
        reservation_id=reservation_id,
        showtime_id=showtime_id,
        seat_id=seat_id,
    )
    db_connection.execute(
        delete(reservations).where(reservations.c.id == reservation_id)
    )
    remaining = db_connection.execute(
        select(func.count())
        .select_from(seat_reservations)
        .where(seat_reservations.c.reservation_id == reservation_id)
    ).scalar_one()
    assert remaining == 0


def test_migration_upgrade_and_downgrade(
    engine: Engine,
    alembic_command: Callable[[str, str], None],
) -> None:
    """Verifies migration 0001 applies and reverts cleanly.

    Runs ``downgrade base`` (dropping every table) then ``upgrade head`` so the
    schema is left at head for any later test. This test uses its own Alembic
    connections, not ``db_connection``, so no test-held transaction is open
    while the DDL runs.
    """
    alembic_command("downgrade", "base")
    assert not inspect(engine).has_table("users")
    alembic_command("upgrade", "head")
    assert inspect(engine).has_table("users")


def test_seat_reservation_showtime_must_match_reservation(
    db_connection: Connection,
) -> None:
    """Verifies FR-7: a seat_reservation's showtime must match its reservation's."""
    movie_id = _insert_movie(db_connection)
    screen_id = _insert_screen(db_connection)
    seat_id = _insert_seat(db_connection, screen_id=screen_id)
    showtime_a = _insert_showtime(db_connection, movie_id=movie_id, screen_id=screen_id)
    showtime_b = _insert_showtime(
        db_connection,
        movie_id=movie_id,
        screen_id=screen_id,
        starts_at=_default_starts_at() + timedelta(hours=3),
    )
    user_id = _insert_user(db_connection)
    reservation_id = _insert_reservation(
        db_connection, user_id=user_id, showtime_id=showtime_a
    )
    # Wrong showtime: the composite FK (reservation_id, showtime_id) rejects it.
    _expect_integrity_error(
        db_connection,
        insert(seat_reservations).values(
            reservation_id=reservation_id,
            showtime_id=showtime_b,
            seat_id=seat_id,
        ),
    )
    # Same row with the reservation's own showtime succeeds.
    _insert_seat_reservation(
        db_connection,
        reservation_id=reservation_id,
        showtime_id=showtime_a,
        seat_id=seat_id,
    )
