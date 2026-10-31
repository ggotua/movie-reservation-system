"""Integration tests for the SPEC-7A showcase seed command.

SPEC-7A Section 5.1 lists five automated checks, and every one of them needs the
showcase dataset to exist first. That dataset is hundreds of rows written to a
real (hosted) test database, so the checks share **one** seeding run instead of
paying for the command five times: a module-scoped fixture deletes any leftover
showcase rows, seeds twice, hands both summaries to the tests, and deletes
everything it created in a ``finally`` block. Seeding twice inside the fixture
is what makes the idempotency claim checkable -- the second summary and the row
counts around it are the evidence, not a second full run of the command.

The fixture seeds a shorter window than the command's fortnight
(:data:`_WINDOW_DAYS`): the same code path, every screen and every slot, but two
days instead of fourteen, so the checks run in minutes rather than the ~45
minutes the full occupancy plan takes over the hosted database. Test 1 asserts
the remaining boundary explicitly -- the command's default window is 14 days and
nothing is seeded on the day after the tests' own window.

The command owns its own transaction by design (:func:`seed_demo` never
commits), so the fixture commits through ``engine.begin()`` and cleans up in a
second transaction rather than rolling back. The tests therefore read the
database through their own ``AsyncEngine``, exactly like the other integration
modules, instead of borrowing the API's connection dependency.

The credential this module seeds with is a throwaway test value, never the demo
account of ``backend/.env``: a test must not depend on, or overwrite, what a
person signs in with.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Table, delete, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import get_settings
from src.core.enums import ACTIVE_RESERVATION_STATUSES
from src.db import seed_demo as showcase
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

# The window these tests seed. The command's default is a fortnight; two days
# keep the single shared seeding run fast while still covering every screen,
# every slot and both the occupancy and demo-reservation steps.
_WINDOW_DAYS: Final[int] = 2

# --- What this module seeds and checks --------------------------------------

# The demo account the fixture creates. The password only has to satisfy the
# SPEC-2 policy: it protects a row the cleanup deletes again (IMPLEMENTER RULES,
# rule 6 -- no real credential in source).
_DEMO_EMAIL: Final[str] = "seed-demo-test@example.invalid"
_DEMO_PASSWORD: Final[str] = "spec-7a test password"
_POSTER_BASE_URL: Final[str] = "http://localhost:5173"

# The committed posters the seed's ``poster_url`` values must point at (SPEC-7A
# D9, FR-7). ``backend/tests/integration`` -> ``backend/tests`` -> ``backend``
# -> repository root.
_POSTER_DIRECTORY: Final[Path] = (
    Path(__file__).resolve().parents[3] / "frontend" / "public" / "posters"
)

# The seat map of each showcase screen (SPEC-7A D4): 8 x 12, 10 x 14, 6 x 10.
_EXPECTED_SEATS: Final[dict[str, int]] = {
    "Hall 1": 96,
    "Hall 2": 140,
    "Hall 3": 60,
}

# The price of every (local hour, screen) pair (SPEC-7A D5): the slot's base
# price, plus 500 cents on the third screen.
_EXPECTED_PRICES: Final[dict[tuple[int, str], int]] = {
    (12, "Hall 1"): 1500,
    (15, "Hall 1"): 2000,
    (18, "Hall 1"): 2500,
    (21, "Hall 1"): 2500,
    (12, "Hall 2"): 1500,
    (15, "Hall 2"): 2000,
    (18, "Hall 2"): 2500,
    (21, "Hall 2"): 2500,
    (12, "Hall 3"): 2000,
    (15, "Hall 3"): 2500,
    (18, "Hall 3"): 3000,
    (21, "Hall 3"): 3000,
}

# Slack the occupancy check allows on top of the D6 band (+-10 points): the plan
# rounds its target to the nearest seat (1), its last booking group can overshoot
# that target by up to three seats, and the demo account may add
# _DEMO_SEATS_PER_RESERVATION seats to a showtime the guests already filled.
_OCCUPANCY_SLACK_SEATS: Final[int] = (
    showcase._GROUP_SIZES[-1] - 1 + showcase._DEMO_SEATS_PER_RESERVATION + 1
)

# Every table the command writes to, in the order the cleanup has to empty them:
# children before the rows they point at.
_TABLES: Final[tuple[Table, ...]] = (
    seat_reservations,
    reservations,
    showtimes,
    seats,
    screens,
    movie_genres,
    movies,
    genres,
    users,
)


def _run_async[ResultT](awaitable: Coroutine[Any, Any, ResultT]) -> ResultT:
    """Run one coroutine to completion, on a selector loop when on Windows.

    async psycopg refuses the ``ProactorEventLoop`` that ``asyncio.run`` selects
    by default on Windows (``python -m src.db.seed_demo`` guards the same way),
    so driving the command from a synchronous fixture needs the same loop.

    Args:
        awaitable: The coroutine to run.

    Returns:
        The value the coroutine returned.
    """
    if sys.platform == "win32":
        return asyncio.run(awaitable, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(awaitable)


@dataclass(frozen=True, slots=True)
class _Showcase:
    """What this module's single seeding run produced, for every test to read.

    Attributes:
        now: The instant both runs were given, so a test can decide which slots
            were in the future without re-reading the clock.
        first: The summary of the run that created the dataset.
        second: The summary of the run against the database the first one left.
        counts_after_first: Row count per table, after run 1.
        counts_after_second: Row count per table, after run 2.
    """

    now: datetime
    first: showcase.SeedSummary
    second: showcase.SeedSummary
    counts_after_first: dict[str, int]
    counts_after_second: dict[str, int]


async def _counts(conn: AsyncConnection) -> dict[str, int]:
    """Return the row count of every table the command writes to.

    Args:
        conn: An open connection; the caller owns the transaction.

    Returns:
        dict[str, int]: ``table name -> row count``.
    """
    counts: dict[str, int] = {}
    for table in _TABLES:
        counts[table.name] = (
            await conn.execute(select(func.count()).select_from(table))
        ).scalar_one()
    return counts


async def _counts_for(engine: AsyncEngine) -> dict[str, int]:
    """Return :func:`_counts` for the test database, on a fresh connection.

    Args:
        engine: The module's async engine.

    Returns:
        dict[str, int]: ``table name -> row count``.
    """
    async with engine.connect() as conn:
        return await _counts(conn)


def _guest_emails() -> list[str]:
    """Return the guest addresses the command creates, lower-cased.

    Returns:
        list[str]: ``guest-1@example.invalid`` to ``guest-8@example.invalid``.
    """
    return [
        showcase._GUEST_EMAIL_TEMPLATE.format(index=index).lower()
        for index in range(1, showcase._GUEST_COUNT + 1)
    ]


def _screen_names() -> list[str]:
    """Return the showcase screen names, in ``_SCREEN_SPECS`` order.

    Returns:
        list[str]: ``["Hall 1", "Hall 2", "Hall 3"]``.
    """
    return [name for name, _, _ in showcase._SCREEN_SPECS]


def _movie_titles() -> list[str]:
    """Return the showcase film titles, in ``_FILMS`` order.

    Returns:
        list[str]: The twelve titles.
    """
    return [film.title for film in showcase._FILMS]


async def _delete_showcase_rows(conn: AsyncConnection) -> None:
    """Delete every row this module's seeding can have created.

    Children come first: ``seat_reservations`` point at reservations, seats and
    showtimes, and ``showtimes``/``seats`` refuse to go while a child still
    points at them. The deletes are written as ``DELETE ... WHERE id IN
    (SELECT ...)`` so they also remove what an *earlier* failed session left
    behind, which is why the fixture runs this before seeding as well.

    Args:
        conn: An open connection inside a transaction the caller commits.
    """
    screen_ids = select(screens.c.id).where(screens.c.name.in_(_screen_names()))
    showtime_ids = select(showtimes.c.id).where(showtimes.c.screen_id.in_(screen_ids))
    movie_ids = select(movies.c.id).where(movies.c.title.in_(_movie_titles()))
    user_ids = select(users.c.id).where(
        func.lower(users.c.email).in_([_DEMO_EMAIL, *_guest_emails()])
    )
    reservation_ids = select(reservations.c.id).where(
        reservations.c.showtime_id.in_(showtime_ids)
        | reservations.c.user_id.in_(user_ids)
    )

    await conn.execute(
        delete(seat_reservations).where(
            seat_reservations.c.reservation_id.in_(reservation_ids)
            | seat_reservations.c.showtime_id.in_(showtime_ids)
        )
    )
    await conn.execute(
        delete(reservations).where(reservations.c.id.in_(reservation_ids))
    )
    await conn.execute(delete(showtimes).where(showtimes.c.id.in_(showtime_ids)))
    await conn.execute(delete(seats).where(seats.c.screen_id.in_(screen_ids)))
    await conn.execute(delete(screens).where(screens.c.id.in_(screen_ids)))
    await conn.execute(
        delete(movie_genres).where(movie_genres.c.movie_id.in_(movie_ids))
    )
    await conn.execute(delete(movies).where(movies.c.id.in_(movie_ids)))
    await conn.execute(delete(genres).where(genres.c.name.in_(showcase._GENRES)))
    await conn.execute(delete(users).where(users.c.id.in_(user_ids)))


async def _delete_showcase_rows_of(engine: AsyncEngine) -> None:
    """Run :func:`_delete_showcase_rows` in its own committed transaction.

    Args:
        engine: The module's async engine.
    """
    async with engine.begin() as conn:
        await _delete_showcase_rows(conn)


def _demo_settings() -> showcase.DemoSeedSettings:
    """Return the settings the command would have loaded from ``.env``.

    The timezone comes from the application settings -- the window has to start
    on the cinema's local day -- while the credentials are this module's
    throwaway values, so a test run never touches the real demo account.

    Returns:
        showcase.DemoSeedSettings: The settings for both runs.
    """
    return showcase.DemoSeedSettings(
        demo_user_email=_DEMO_EMAIL,
        demo_user_password=_DEMO_PASSWORD,
        poster_base_url=_POSTER_BASE_URL,
        cinema_timezone=get_settings().cinema_timezone,
    )


async def _seed_twice(engine: AsyncEngine) -> _Showcase:
    """Delete leftovers, seed twice and count rows around both runs.

    Args:
        engine: The module's async engine.

    Returns:
        _Showcase: Both summaries, the fixed clock and the two row counts.
    """
    settings = _demo_settings()
    async with engine.begin() as conn:
        await _delete_showcase_rows(conn)
    async with engine.begin() as conn:
        now = datetime.now(UTC)
        first = await showcase.seed_demo(conn, settings, now, days=_WINDOW_DAYS)
        counts_after_first = await _counts(conn)
    async with engine.begin() as conn:
        second = await showcase.seed_demo(conn, settings, now, days=_WINDOW_DAYS)
        counts_after_second = await _counts(conn)
    return _Showcase(
        now=now,
        first=first,
        second=second,
        counts_after_first=counts_after_first,
        counts_after_second=counts_after_second,
    )


@pytest.fixture(scope="module")
def showcase_engine(_guard_test_database: str) -> Iterator[AsyncEngine]:
    """Yield an async engine bound to the test database.

    ``NullPool`` is deliberate: every :func:`_run_async` call drives its own
    event loop, and a pooled async connection is bound to the loop that opened
    it, so a connection kept between two calls would be used from the wrong
    loop. With ``NullPool`` each call opens and closes its own connection.

    Args:
        _guard_test_database: The validated test URL, already checked to end
            with ``_test``.

    Yields:
        AsyncEngine: An engine with ``pool_pre_ping`` enabled, disposed after
        the module.
    """
    engine = create_async_engine(
        _guard_test_database, pool_pre_ping=True, poolclass=NullPool
    )
    try:
        yield engine
    finally:
        engine.sync_engine.dispose()


@pytest.fixture(scope="module")
def showcase_run(
    showcase_engine: AsyncEngine, migrated_schema: None
) -> Iterator[_Showcase]:
    """Seed the showcase dataset twice, once for the whole module.

    Args:
        showcase_engine: The module's async engine.
        migrated_schema: Ensures ``alembic upgrade head`` has run first.

    Yields:
        _Showcase: Both summaries and the row counts around them.
    """
    state = _run_async(_seed_twice(showcase_engine))
    try:
        yield state
    finally:
        _run_async(_delete_showcase_rows_of(showcase_engine))


def _total_window_slots() -> int:
    """Return how many (day, screen, slot) combinations the window covers.

    Returns:
        int: ``_WINDOW_DAYS * screens * slots``.
    """
    return _WINDOW_DAYS * len(showcase._SCREEN_SPECS) * len(showcase._SLOTS)


def _future_window_slots(now: datetime, zone: ZoneInfo) -> int:
    """Return how many window slots start after ``now``, screens included.

    Args:
        now: The instant both seeding runs were given.
        zone: The cinema's timezone, which decides the window's first day.

    Returns:
        int: The number of showtimes the plan can create.
    """
    today = now.astimezone(zone).date()
    future_slots = 0
    for day_offset in range(_WINDOW_DAYS):
        day = today + timedelta(days=day_offset)
        for hour, _ in showcase._SLOTS:
            if datetime.combine(day, time(hour), tzinfo=zone) > now:
                future_slots += 1
    return future_slots * len(showcase._SCREEN_SPECS)


async def test_seed_demo_creates_expected_catalog_counts(
    showcase_run: _Showcase, showcase_engine: AsyncEngine
) -> None:
    """SPEC-7A test 1: 6 genres, 12 films, 3 screens, future slots, 12 posters."""
    zone = ZoneInfo(get_settings().cinema_timezone)
    first = showcase_run.first
    async with showcase_engine.connect() as conn:
        genre_names = set(
            (
                await conn.execute(
                    select(genres.c.name).where(genres.c.name.in_(showcase._GENRES))
                )
            ).scalars()
        )
        movie_rows = (
            await conn.execute(
                select(movies.c.title, movies.c.poster_url).where(
                    movies.c.title.in_(_movie_titles())
                )
            )
        ).all()
        pair_rows = (
            await conn.execute(
                select(movies.c.title, genres.c.name)
                .select_from(
                    movie_genres.join(
                        movies, movies.c.id == movie_genres.c.movie_id
                    ).join(genres, genres.c.id == movie_genres.c.genre_id)
                )
                .where(movies.c.title.in_(_movie_titles()))
            )
        ).all()
        seat_rows = (
            await conn.execute(
                select(screens.c.name, func.count(seats.c.id))
                .select_from(
                    screens.outerjoin(seats, seats.c.screen_id == screens.c.id)
                )
                .where(screens.c.name.in_(_screen_names()))
                .group_by(screens.c.name)
            )
        ).all()
        showtime_rows = (
            await conn.execute(
                select(showtimes.c.starts_at, showtimes.c.price_cents, screens.c.name)
                .select_from(
                    showtimes.join(screens, screens.c.id == showtimes.c.screen_id)
                )
                .where(screens.c.name.in_(_screen_names()))
            )
        ).all()

    assert first.genres_created == len(showcase._GENRES)
    assert first.movies_created == len(showcase._FILMS)
    assert first.screens_created == len(showcase._SCREEN_SPECS)
    assert first.showtimes_skipped == 0
    assert first.showtimes_past_skipped == (
        _total_window_slots() - _future_window_slots(showcase_run.now, zone)
    )
    # These tests seed a short window for speed (see _WINDOW_DAYS above), so the
    # command's own window, SPEC-7A D4's fortnight today .. today+13, is checked
    # on the default value the command uses rather than by seeding 14 days.
    assert showcase._WINDOW_DAYS == 14

    assert genre_names == set(showcase._GENRES)
    assert {title for title, _ in movie_rows} == set(_movie_titles())
    assert {(title, genre) for title, genre in pair_rows} == {
        (film.title, genre) for film in showcase._FILMS for genre in film.genre_names
    }
    assert {(name, count) for name, count in seat_rows} == set(_EXPECTED_SEATS.items())

    poster_files: set[str] = set()
    for title, poster_url in movie_rows:
        assert poster_url is not None
        assert poster_url == showcase.poster_url(_POSTER_BASE_URL, title)
        poster_files.add(Path(poster_url).name)
    assert len(poster_files) == len(showcase._FILMS)
    on_disk = {path.name for path in _POSTER_DIRECTORY.glob("*.svg")}
    assert poster_files == on_disk

    assert len(showtime_rows) == _future_window_slots(showcase_run.now, zone)
    assert first.showtimes_created == len(showtime_rows)
    assert all(starts_at > showcase_run.now for starts_at, _, _ in showtime_rows)
    window_days = {
        showcase_run.now.astimezone(zone).date() + timedelta(days=offset)
        for offset in range(_WINDOW_DAYS)
    }
    seeded_days = {
        starts_at.astimezone(zone).date() for starts_at, _, _ in showtime_rows
    }
    # Every seeded day is inside the window and the day after it is never
    # seeded, which is SPEC-7A Section 5.2's window boundary at this length.
    assert seeded_days <= window_days
    assert max(seeded_days) == max(window_days)
    prices = {
        (starts_at.astimezone(zone).hour, name): price_cents
        for starts_at, price_cents, name in showtime_rows
    }
    assert prices == _EXPECTED_PRICES


def test_seed_demo_is_idempotent_second_run_adds_nothing(
    showcase_run: _Showcase,
) -> None:
    """SPEC-7A test 2: run 2 creates no row and changes no row count."""
    first = showcase_run.first
    second = showcase_run.second

    assert showcase_run.counts_after_second == showcase_run.counts_after_first
    assert second.genres_created == 0
    assert second.movies_created == 0
    assert second.screens_created == 0
    assert second.showtimes_created == 0
    assert second.guests_created == 0
    assert second.showtimes_booked == 0
    assert second.guest_reservations_created == 0
    assert second.seats_taken == 0
    assert second.demo_user_created is False
    assert second.demo_reservations_created == 0

    # The skips of run 2 are exactly the rows run 1 created.
    assert first.showtimes_already_booked == 0
    assert first.demo_reservations_created == len(showcase._DEMO_RESERVATIONS)
    assert second.showtimes_skipped == first.showtimes_created
    assert second.showtimes_past_skipped == first.showtimes_past_skipped
    assert second.showtimes_already_booked == first.showtimes_created
    assert second.guests_skipped == showcase._GUEST_COUNT
    assert second.demo_reservations_skipped == len(showcase._DEMO_RESERVATIONS)


async def test_seed_demo_respects_rules_after_seeding(
    showcase_run: _Showcase, showcase_engine: AsyncEngine
) -> None:
    """SPEC-7A test 3: catalog rules and the D6 band hold on the seeded rows."""
    zone = ZoneInfo(get_settings().cinema_timezone)
    slot_hours = {hour: index for index, (hour, _) in enumerate(showcase._SLOTS)}
    async with showcase_engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    showtimes.c.id,
                    showtimes.c.starts_at,
                    screens.c.name,
                    func.count(seats.c.id),
                )
                .select_from(
                    showtimes.join(screens, screens.c.id == showtimes.c.screen_id).join(
                        seats, seats.c.screen_id == screens.c.id
                    )
                )
                .where(screens.c.name.in_(_screen_names()))
                .group_by(showtimes.c.id, showtimes.c.starts_at, screens.c.name)
                .order_by(screens.c.name, showtimes.c.starts_at)
            )
        ).all()
        taken_rows = (
            await conn.execute(
                select(seat_reservations.c.showtime_id, func.count())
                .where(seat_reservations.c.status.in_(ACTIVE_RESERVATION_STATUSES))
                .group_by(seat_reservations.c.showtime_id)
            )
        ).all()
        duplicated = (
            await conn.execute(
                select(seat_reservations.c.showtime_id, seat_reservations.c.seat_id)
                .where(seat_reservations.c.status.in_(ACTIVE_RESERVATION_STATUSES))
                .group_by(seat_reservations.c.showtime_id, seat_reservations.c.seat_id)
                .having(func.count() > 1)
            )
        ).all()

    assert rows
    assert duplicated == []

    taken: dict[int, int] = {}
    for showtime_id, count in taken_rows:
        taken[showtime_id] = count
    starts_by_screen: dict[str, list[datetime]] = {}
    for showtime_id, starts_at, name, seat_count in rows:
        assert starts_at > showcase_run.now, f"showtime {showtime_id} is in the past"
        starts_by_screen.setdefault(name, []).append(starts_at)
        hour = starts_at.astimezone(zone).hour
        assert hour in slot_hours, f"showtime {showtime_id} is not on a seeded slot"
        share = showcase._OCCUPANCY_SHARES[slot_hours[hour]]
        occupied = taken.get(showtime_id, 0)
        assert occupied > 0, f"showtime {showtime_id} sold no seat"
        assert occupied < seat_count, f"showtime {showtime_id} is sold out"
        assert occupied >= seat_count * (share - showcase._OCCUPANCY_JITTER)
        assert occupied <= (
            seat_count * (share + showcase._OCCUPANCY_JITTER) + _OCCUPANCY_SLACK_SEATS
        )

    # No two showtimes on one screen are closer than the catalog allows.
    assert set(starts_by_screen) == set(_screen_names())
    slot_minutes = get_settings().showtime_slot_minutes
    for instants in starts_by_screen.values():
        for earlier, later in pairwise(sorted(instants)):
            assert later - earlier >= timedelta(minutes=slot_minutes)


async def test_seed_demo_demo_user_has_three_reservations(
    showcase_run: _Showcase, showcase_engine: AsyncEngine
) -> None:
    """SPEC-7A test 4: the demo account owns two confirmed and one cancelled."""
    async with showcase_engine.connect() as conn:
        demo_user_id = (
            await conn.execute(
                select(users.c.id).where(func.lower(users.c.email) == _DEMO_EMAIL)
            )
        ).scalar_one()
        rows = (
            await conn.execute(
                select(
                    reservations.c.status,
                    reservations.c.confirmed_at,
                    reservations.c.cancelled_at,
                    showtimes.c.starts_at,
                    func.count(seat_reservations.c.id),
                )
                .select_from(
                    reservations.join(
                        showtimes, showtimes.c.id == reservations.c.showtime_id
                    ).outerjoin(
                        seat_reservations,
                        seat_reservations.c.reservation_id == reservations.c.id,
                    )
                )
                .where(reservations.c.user_id == demo_user_id)
                .group_by(
                    reservations.c.id,
                    reservations.c.status,
                    reservations.c.confirmed_at,
                    reservations.c.cancelled_at,
                    showtimes.c.starts_at,
                )
            )
        ).all()

    assert len(rows) == len(showcase._DEMO_RESERVATIONS)
    assert sorted(status for status, _, _, _, _ in rows) == [
        "cancelled",
        "confirmed",
        "confirmed",
    ]
    for status, confirmed_at, cancelled_at, starts_at, seat_count in rows:
        assert starts_at > showcase_run.now, "every demo reservation is upcoming"
        assert seat_count == showcase._DEMO_SEATS_PER_RESERVATION
        assert confirmed_at is not None, f"{status} reservation was never confirmed"
        if status == "cancelled":
            assert cancelled_at is not None
        else:
            assert cancelled_at is None


def _run_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    database_url: str,
    demo_user_email: str,
    demo_user_password: str,
) -> tuple[int, str]:
    """Run the command once with the given environment.

    ``main`` is called in-process rather than as a subprocess so the test can
    read the exit code and the message. ``get_settings`` is cleared before and
    after the call, because the command reads ``DATABASE_URL`` through it and
    the patch only lasts for this test.

    Args:
        monkeypatch: Patches the environment.
        capsys: Captures what the command printed.
        database_url: The ``DATABASE_URL`` to run against.
        demo_user_email: The ``DEMO_USER_EMAIL`` to run with.
        demo_user_password: The ``DEMO_USER_PASSWORD`` to run with; an empty
            string is how a "set but blank" value reaches the guard.

    Returns:
        tuple[int, str]: The exit code and whatever the command wrote to stderr.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DEMO_USER_EMAIL", demo_user_email)
    monkeypatch.setenv("DEMO_USER_PASSWORD", demo_user_password)
    get_settings.cache_clear()
    try:
        exit_code = showcase.main()
        captured = capsys.readouterr()
    finally:
        get_settings.cache_clear()
    return exit_code, captured.err


def test_seed_demo_refuses_test_database_name_and_missing_password(
    showcase_engine: AsyncEngine,
    _guard_test_database: str,
    migrated_schema: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SPEC-7A test 5: both refusals stop before any write and say why.

    The showcase fixture is deliberately *not* requested here: the checks are
    about refusing to run, so they must hold whether or not the showcase rows
    exist, and they stay fast (two in-process guard calls, two row counts).
    """
    before = _run_async(_counts_for(showcase_engine))

    test_database_exit, test_database_error = _run_command(
        monkeypatch,
        capsys,
        database_url=_guard_test_database,
        demo_user_email=_DEMO_EMAIL,
        demo_user_password=_DEMO_PASSWORD,
    )
    assert test_database_exit == 1
    assert "test database" in test_database_error

    missing_password_exit, missing_password_error = _run_command(
        monkeypatch,
        capsys,
        database_url=(
            "postgresql+psycopg://seed:placeholder@localhost:5432/movie_reservation"
        ),
        demo_user_email=_DEMO_EMAIL,
        demo_user_password="",
    )
    assert missing_password_exit == 1
    assert "DEMO_USER_PASSWORD" in missing_password_error
    assert "is not set" in missing_password_error

    after = _run_async(_counts_for(showcase_engine))
    assert after == before
