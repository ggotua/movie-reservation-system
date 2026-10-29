"""Shared pytest fixtures for the Movie Reservation System backend test suite.

These fixtures back the database-constraint integration tests in
``tests/integration/test_schema.py``, which prove that the constraints defined
in ``src/db/tables.py`` (SPEC-1) are enforced by PostgreSQL itself, not by
Python. Because the tests must exercise real database constraints, they run
against a real PostgreSQL server and never mock the database.

Target database
---------------
The tests connect to the URL in the environment variable ``TEST_DATABASE_URL``,
read the same way ``src/db/seed.py`` reads its settings: a ``pydantic-settings``
``BaseSettings`` subclass that loads ``backend/.env`` (commands run from
``backend/``) and then real environment variables (which take precedence), and
fails loudly when the value is missing. There is deliberately no Docker and no
``testcontainers`` dependency: the developer's machine cannot run
virtualization, and CI provides a PostgreSQL service container instead.

Safety
------
``test_migration_upgrade_and_downgrade`` runs ``alembic downgrade base``, which
drops every table. A session-scoped guard fixture therefore REFUSES to run the
suite (it raises, it does not skip) unless the database named in
``TEST_DATABASE_URL`` ends with ``_test``. ``DATABASE_URL`` is never used here.

Engine style
------------
The SPEC-1 constraint tests use a synchronous SQLAlchemy engine
(``create_engine`` on the ``postgresql+psycopg://`` URL) plus plain synchronous
fixtures: they only prove database constraints, so async would add cost without
adding coverage.

The SPEC-2 auth API tests in ``tests/integration/test_auth_api.py`` drive the
real application through an ASGI client, so they need an async engine, an async
connection and the fixtures described below.

Windows event loop (SPEC-2 Section 2.8)
---------------------------------------
Async psycopg cannot run on Windows' ``ProactorEventLoop``, which is the loop
pytest-asyncio creates there by default. The ``pytest_asyncio_loop_factories``
hook implementation below -- the replacement pytest-asyncio 1.4 recommends for
the deprecated ``event_loop_policy`` fixture -- makes every async test and
async fixture run on a Selector-based loop when ``sys.platform`` is ``"win32"``.
On every other platform that hook is not defined at all, so pytest-asyncio's
own default loop is used unchanged. The synchronous SPEC-1 tests are unaffected
either way.

JWT settings
------------
The auth API tests sign and verify real tokens, so the application must read a
known secret and a known lifetime instead of inheriting whatever
``backend/.env`` holds: the session-scoped ``_jwt_environment`` fixture sets
``JWT_SECRET`` and ``JWT_EXPIRY_MINUTES`` in the environment, clears the
``get_settings`` cache on both sides of the change (the pattern
:func:`_run_alembic` already uses for ``DATABASE_URL``) and restores the
previous values, or removes them when they were unset.

Database targeting and isolation
--------------------------------
Every fixture here connects to ``TEST_DATABASE_URL`` only, never to
``DATABASE_URL``, and the session-scoped guard fixture refuses to run unless
that URL names a ``*_test`` database. The API fixtures give every request of one
test ONE connection inside an outer transaction that is rolled back at teardown,
so nothing a test writes is committed.

SPEC-3 helpers
--------------
The bottom of this file adds the fixtures the SPEC-3 API tests share
(``tests/integration/test_movies_api.py`` and ``test_showtimes_api.py``): an
acting administrator (``admin_headers``), a ``now``-relative instant builder
(``future_start``) and the three setup calls every write needs -- a genre-free
movie, a screen with its generated seats and a showtime
(``create_movie_via_api``, ``create_screen_via_api``, ``create_showtime_via_api``).
They live here rather than in either module because both use them, and each one
asserts the status it requires, so a test that only cares about its last request
never has to re-check its setup.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Table, create_engine, func, insert, select, update
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from src.core.config import get_settings
from src.core.enums import (
    ACTIVE_RESERVATION_STATUSES,
    ADMIN_USER_ROLE,
    DEFAULT_RESERVATION_STATUS,
    RESERVATION_STATUSES,
)
from src.db.tables import reservations, seat_reservations, seats, showtimes, users

# backend/ — this file lives at backend/tests/conftest.py.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _BACKEND_DIR / "alembic.ini"
_MIGRATIONS_DIR = _BACKEND_DIR / "migrations"


class _TestSettings(BaseSettings):
    """The test-only database URL, mirroring ``src/db/seed.py``'s settings.

    Loads ``backend/.env`` when present and then real environment variables
    (which take precedence), and fails loudly when ``TEST_DATABASE_URL`` is
    unset or blank, so a misconfigured run never silently targets the wrong
    database.

    Attributes:
        test_database_url: A synchronous ``postgresql+psycopg://`` URL naming a
            throwaway database whose name ends with ``_test``. No default.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    test_database_url: str = Field(..., alias="TEST_DATABASE_URL", min_length=1)

    @field_validator("test_database_url")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Reject a whitespace-only ``TEST_DATABASE_URL``.

        Args:
            value: The raw value supplied via the environment.

        Returns:
            str: The unchanged value when it contains non-whitespace.

        Raises:
            ValueError: If ``value`` is empty or only whitespace.
        """
        if not value.strip():
            raise ValueError("must not be blank")
        return value


def _load_test_database_url() -> str:
    """Return the ``TEST_DATABASE_URL`` value, failing loudly when unset.

    Returns:
        str: The synchronous psycopg URL of the test database.

    Raises:
        pydantic.ValidationError: If ``TEST_DATABASE_URL`` is unset or blank.
    """
    return _TestSettings().test_database_url


def _run_alembic(action: str, revision: str) -> None:
    """Run an Alembic ``upgrade``/``downgrade`` against the test database.

    ``migrations/env.py`` reads the connection URL through
    ``get_settings().database_url``, so this points that setting at
    ``TEST_DATABASE_URL`` for the duration of the call: it saves the current
    ``DATABASE_URL`` (if any), sets it to the test URL, clears the
    ``get_settings`` cache, and restores both afterwards.

    Args:
        action: Either ``"upgrade"`` or ``"downgrade"``.
        revision: The target revision, e.g. ``"head"`` or ``"base"``.

    Raises:
        ValueError: If ``action`` is neither ``"upgrade"`` nor ``"downgrade"``.
    """
    if action not in ("upgrade", "downgrade"):
        raise ValueError(f"unsupported alembic action: {action!r}")

    saved_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _load_test_database_url()
    get_settings.cache_clear()
    try:
        config = Config(str(_ALEMBIC_INI))
        # Absolute script_location so resolution does not depend on the CWD
        # the test session was started from.
        config.set_main_option("script_location", str(_MIGRATIONS_DIR))
        if action == "upgrade":
            command.upgrade(config, revision)
        else:
            command.downgrade(config, revision)
    finally:
        if saved_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved_database_url
        get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _guard_test_database() -> str:
    """Refuse to run unless ``TEST_DATABASE_URL`` names a ``*_test`` database.

    Session-scoped and autouse, so it runs before any test (in particular
    before the migration test that drops every table). It raises — it does not
    skip — so a misconfigured run fails loudly instead of destroying a real
    database.

    Returns:
        str: The validated test database URL, reused by the other fixtures.

    Raises:
        RuntimeError: If the database name does not end with ``_test``.
    """
    url = _load_test_database_url()
    database = make_url(url).database
    if database is None or not database.endswith("_test"):
        raise RuntimeError(
            "Refusing to run database tests: TEST_DATABASE_URL must name a "
            f"database ending in '_test' (got database name {database!r}). "
            "test_migration_upgrade_and_downgrade drops every table."
        )
    return url


@pytest.fixture(scope="session")
def engine(_guard_test_database: str) -> Iterator[Engine]:
    """Yield a synchronous engine bound to the test database.

    Args:
        _guard_test_database: The validated test URL, already checked to end
            with ``_test``.

    Yields:
        Engine: A synchronous ``postgresql+psycopg`` engine with
        ``pool_pre_ping`` enabled (hosted Neon can drop idle connections).
    """
    test_engine = create_engine(_guard_test_database, pool_pre_ping=True)
    try:
        yield test_engine
    finally:
        test_engine.dispose()


@pytest.fixture(scope="session")
def migrated_schema(_guard_test_database: str) -> None:
    """Apply the real Alembic migration (``upgrade head``) once per session.

    Every test therefore runs against the schema produced by migration ``0001``,
    so the tests exercise the migration as well as the constraints.

    Args:
        _guard_test_database: The validated test URL.
    """
    _run_alembic("upgrade", "head")


@pytest.fixture(scope="session")
def alembic_command(_guard_test_database: str) -> Callable[[str, str], None]:
    """Return a callable that runs an Alembic action against the test database.

    Provided for the migration test, which performs its own ``downgrade base``
    then ``upgrade head`` cycle.

    Args:
        _guard_test_database: The validated test URL.

    Returns:
        Callable[[str, str], None]: ``run(action, revision)`` bound to this
        test session.
    """

    def run(action: str, revision: str) -> None:
        """Run one Alembic action against the test database."""
        _run_alembic(action, revision)

    return run


@pytest.fixture
def db_connection(engine: Engine, migrated_schema: None) -> Iterator[Connection]:
    """Yield a per-test connection wrapped in a transaction that is rolled back.

    Isolation strategy: open one connection, start a transaction, hand it to
    the test, then roll back and close. This keeps the schema in place (unlike
    dropping and recreating tables) and keeps the number of network round trips
    low (the connection is reused from the engine's pool).

    Args:
        engine: The session-scoped test engine.
        migrated_schema: Ensures ``alembic upgrade head`` has run.

    Yields:
        Connection: A connection inside a transaction, rolled back on teardown.
    """
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


# --- SPEC-2 auth API: loop policy, settings, engine, connections ------------
#
# SPEC-2 Section 5 drives the API through an ASGI client instead of a live
# server, so these fixtures are async and read TEST_DATABASE_URL -- the same
# setting the synchronous fixtures above use. DATABASE_URL is never read here:
# the application's own module-level engine stays unused because every request
# of a test runs through an overridden ``get_connection``.

# The JWT settings the API is forced to read for the whole session. The secret
# is deliberately NOT a secret: it only signs throwaway tokens while the test
# session runs, and it must satisfy the >= 32 character rule in
# src/core/config.py. Like the CI workflow's values, it protects nothing.
TEST_JWT_SECRET = "test-suite-only-jwt-secret-not-a-secret-0123456789"
TEST_JWT_EXPIRY_MINUTES = 60

# The conventional host for a client that talks to an ASGI app in-process.
_BASE_URL = "http://test"

_JWT_ENV_VARS = ("JWT_SECRET", "JWT_EXPIRY_MINUTES")


if sys.platform == "win32":

    def pytest_asyncio_loop_factories(
        config: pytest.Config,
        item: pytest.Item,
    ) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
        """Run every async test and fixture on a Selector-based event loop.

        Async psycopg cannot run on Windows' ``ProactorEventLoop``, which is
        what pytest-asyncio creates there by default, so every database-backed
        async test would fail (SPEC-2 Section 2.8). pytest-asyncio 1.4
        deprecates overriding its ``event_loop_policy`` fixture in favour of
        this hook, and the hook is used for that reason. The factory is the same
        loop ``src.main.run()`` starts the development server on.

        Defined only on Windows: elsewhere no implementation is registered, so
        pytest-asyncio's own default loop is used unchanged. The synchronous
        SPEC-1 tests are unaffected either way.

        Args:
            config: The pytest configuration; unused, present because the hook
                specification passes it.
            item: The test item the loop factory is chosen for; unused.

        Returns:
            Mapping[str, Callable[[], AbstractEventLoop]]: The single factory
            named ``"selector"``, which builds a fresh
            :class:`asyncio.SelectorEventLoop`.
        """
        return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture(scope="session")
def _jwt_environment() -> Iterator[None]:
    """Force a known JWT secret and lifetime for the whole test session.

    Both environment variables are set for the duration of the session and the
    ``get_settings`` cache is cleared on either side of the change, so the
    application reads the test values and the previous values are restored
    exactly -- or removed when they were unset.

    Yields:
        None: Control returns to the test session with the variables in place.
    """
    saved = {name: os.environ.get(name) for name in _JWT_ENV_VARS}
    os.environ["JWT_SECRET"] = TEST_JWT_SECRET
    os.environ["JWT_EXPIRY_MINUTES"] = str(TEST_JWT_EXPIRY_MINUTES)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def jwt_secret(_jwt_environment: None) -> str:
    """Return the HS256 secret the API signs tokens with during the session.

    Exposed as a fixture rather than read from the environment by a test, so
    the tests never handle credentials themselves.

    Args:
        _jwt_environment: Ensures the variable is set before its value is used.

    Returns:
        str: The value of ``JWT_SECRET`` for this session.
    """
    return TEST_JWT_SECRET


@pytest.fixture(scope="session")
def jwt_expiry_minutes(_jwt_environment: None) -> int:
    """Return the token lifetime the API applies during the session.

    Args:
        _jwt_environment: Ensures the variable is set before its value is used.

    Returns:
        int: The value of ``JWT_EXPIRY_MINUTES`` for this session.
    """
    return TEST_JWT_EXPIRY_MINUTES


def _create_app() -> FastAPI:
    """Build the application under test, importing ``src.main`` lazily.

    ``src.main`` imports ``src.db.engine``, which builds an engine from
    ``DATABASE_URL`` when it is first imported. Importing it here rather than at
    module scope keeps a database-less unit-test run independent of that
    variable, and the API fixtures never use the application's own engine in any
    case: every request runs through an overridden ``get_connection``
    (SPEC-2 Section 2.7).

    Returns:
        FastAPI: A fresh instance carrying the real routers and error handlers.
    """
    from src.main import create_app

    return create_app()


def _connection_dependency() -> Callable[[], AsyncIterator[AsyncConnection]]:
    """Return the application's ``get_connection``, imported lazily.

    The override must be keyed by the exact object the routes' ``DbConnection``
    alias resolves to, or FastAPI would not find the override. The lazy import
    has the same reason as in :func:`_create_app`.

    Returns:
        Callable[[], AsyncIterator[AsyncConnection]]: ``get_connection`` itself.
    """
    from src.db.engine import get_connection

    return get_connection


def _asgi_client(app: FastAPI) -> httpx.AsyncClient:
    """Build a client that calls an ASGI application in-process.

    ``httpx.ASGITransport`` sends each request straight into the application, so
    no server, port or socket is involved; the caller owns the client's
    lifetime. The client must be used inside the test's event loop.

    Args:
        app: The application to send requests to.

    Returns:
        httpx.AsyncClient: An unopened client for ``app``.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=_BASE_URL,
    )


@pytest.fixture
async def async_engine(
    _guard_test_database: str,
    migrated_schema: None,
) -> AsyncIterator[AsyncEngine]:
    """Yield an async engine bound to the test database.

    The ``postgresql+psycopg://`` URL the synchronous fixtures use works for
    async psycopg 3 as well, so no second database setting is introduced
    (SPEC-2 Sections 2.7, 5). ``migrated_schema`` is a dependency because a
    fresh CI database has no tables until ``alembic upgrade head`` has run.

    Args:
        _guard_test_database: The validated test URL, already checked to end
            with ``_test``.
        migrated_schema: Ensures the schema exists before the first request.

    Yields:
        AsyncEngine: An engine whose pool is disposed when the test ends.
    """
    test_engine = create_async_engine(_guard_test_database, pool_pre_ping=True)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest.fixture
async def rollback_connection(
    async_engine: AsyncEngine,
) -> AsyncIterator[AsyncConnection]:
    """Yield ONE connection inside an outer transaction that is rolled back.

    Nothing a test does through this connection is ever committed: the
    transaction is rolled back and the connection closed when the test ends. The
    connection is also the one the API fixtures below hand to every request of
    that test, which is what lets a test read a row a request just wrote -- and
    lets a write made by the test be visible to the next request -- without a
    single commit.

    Args:
        async_engine: The per-test async engine.

    Yields:
        AsyncConnection: A connection whose outer transaction is rolled back at
        teardown.
    """
    connection = await async_engine.connect()
    transaction = await connection.begin()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.fixture
async def app_with_rollback_connection(
    rollback_connection: AsyncConnection,
    _jwt_environment: None,
) -> AsyncIterator[FastAPI]:
    """Yield the app with ``get_connection`` overridden by the shared connection.

    The override is a generator that yields the test's single rolled-back
    connection for every request, so all requests in one test share one
    connection and one transaction and observe each other's uncommitted writes.
    Nothing is committed, so the database is left exactly as it was.

    Args:
        rollback_connection: The one connection every request must use.
        _jwt_environment: Ensures the JWT settings are in place before any
            request is served.

    Yields:
        FastAPI: The application, with the override installed on this instance
        only.
    """
    app = _create_app()

    async def _override() -> AsyncIterator[AsyncConnection]:
        """Yield the test's shared, never-committed connection."""
        yield rollback_connection

    app.dependency_overrides[_connection_dependency()] = _override
    yield app


@pytest.fixture
async def client(
    app_with_rollback_connection: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an httpx client that calls the application in-process over ASGI.

    Args:
        app_with_rollback_connection: The application to send requests to.

    Yields:
        httpx.AsyncClient: A client bound to the rolled-back application.
    """
    async with _asgi_client(app_with_rollback_connection) as http_client:
        yield http_client


@pytest.fixture
async def app_with_committing_connection(
    async_engine: AsyncEngine,
    _jwt_environment: None,
) -> AsyncIterator[FastAPI]:
    """Yield the app with ``get_connection`` overridden by real commits.

    This override mirrors the production dependency exactly: a NEW connection
    and a NEW transaction per request, committed when the request returns and
    rolled back when it raises (SPEC-2 Section 2.7). Only the concurrent-signup
    test uses it -- two simultaneous requests must not share one transaction, or
    the loser would never observe the unique-index conflict the winner creates
    -- and that test therefore has to clean up after itself.

    Args:
        async_engine: The per-test engine the requests draw connections from.
        _jwt_environment: Ensures the JWT settings are in place before any
            request is served.

    Yields:
        FastAPI: The application, with the override installed on this instance
        only.
    """
    app = _create_app()

    async def _override() -> AsyncIterator[AsyncConnection]:
        """Yield one connection per request, committed or rolled back."""
        async with async_engine.begin() as connection:
            yield connection

    app.dependency_overrides[_connection_dependency()] = _override
    yield app


@pytest.fixture
async def committing_client(
    app_with_committing_connection: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an ASGI client whose application commits every request.

    Args:
        app_with_committing_connection: The committing application.

    Yields:
        httpx.AsyncClient: A client bound to the committing application.
    """
    async with _asgi_client(app_with_committing_connection) as http_client:
        yield http_client


async def _create_user(
    client: httpx.AsyncClient,
    email: str,
    password: str,
) -> httpx.Response:
    """Sign an account up through the API (SPEC-2 FR-1).

    Args:
        client: The ASGI client bound to the application under test.
        email: The address to register, in any case; the service normalizes it.
        password: The plain-text password, which must satisfy the policy unless
            the test is asserting the rejection.

    Returns:
        httpx.Response: The raw ``POST /auth/signup`` response, so a test can
        assert on a 201 body or on any rejection body.
    """
    return await client.post(
        "/auth/signup",
        json={"email": email, "password": password},
    )


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    """Log in and return the access token (SPEC-2 FR-4).

    Args:
        client: The ASGI client bound to the application under test.
        email: The account's address.
        password: The account's plain-text password.

    Returns:
        str: The ``access_token`` from the response body.

    Raises:
        AssertionError: If the credentials are not accepted, so a test that
            expects a successful login never continues with an empty token. The
            response body is attached to the failure for diagnosis.
    """
    response = await client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def _make_admin(conn: AsyncConnection, user_id: int) -> None:
    """Give an account the ``admin`` role directly in the database.

    Used to set up an acting administrator, and to promote a user behind the
    API's back, without spending an extra HTTP round trip. The change belongs to
    the caller's transaction, so a test that passes the rolled-back connection
    leaves no trace behind.

    Args:
        conn: The open connection the test shares with the application.
        user_id: The account's primary key.
    """
    await conn.execute(
        update(users).where(users.c.id == user_id).values(role=ADMIN_USER_ROLE)
    )


def _auth_header(token: str) -> dict[str, str]:
    """Build the bearer ``Authorization`` header for a token.

    Args:
        token: The access token, exactly as ``POST /auth/login`` returned it.

    Returns:
        dict[str, str]: A one-entry header mapping for ``httpx``.
    """
    return {"Authorization": f"Bearer {token}"}


# The helpers above are plain functions; these fixtures hand them to a test
# without a test module ever importing this one (which would need a package
# ``tests`` does not have). Each fixture takes no arguments and returns the
# function itself, so the call signatures stay exactly as written above.
_CreateUser = Callable[[httpx.AsyncClient, str, str], Awaitable[httpx.Response]]
_Login = Callable[[httpx.AsyncClient, str, str], Awaitable[str]]
_MakeAdmin = Callable[[AsyncConnection, int], Awaitable[None]]
_AuthHeader = Callable[[str], dict[str, str]]


@pytest.fixture
def create_user() -> _CreateUser:
    """Expose :func:`_create_user` as ``create_user(client, email, password)``.

    Returns:
        _CreateUser: The signup helper.
    """
    return _create_user


@pytest.fixture
def login() -> _Login:
    """Expose :func:`_login` as ``login(client, email, password) -> token``.

    Tests that assert on a *failed* login call ``client.post`` directly, since
    this helper would raise on any status other than 200.

    Returns:
        _Login: The login helper.
    """
    return _login


@pytest.fixture
def make_admin() -> _MakeAdmin:
    """Expose :func:`_make_admin` as ``make_admin(conn, user_id)``.

    Returns:
        _MakeAdmin: The role-change helper.
    """
    return _make_admin


@pytest.fixture
def auth_header() -> _AuthHeader:
    """Expose :func:`_auth_header` as ``auth_header(token)``.

    Returns:
        _AuthHeader: The header-builder helper.
    """
    return _auth_header


# --- SPEC-3 setup helpers (movies, screens and showtimes) ------------------
#
# Every SPEC-3 write needs an acting administrator, and almost every showtime
# test needs a movie and a screen to schedule on, so those four calls live here
# instead of being repeated in both SPEC-3 test modules. Each helper asserts the
# status it requires and returns the parsed body, so a test that only cares about
# its final request never has to re-check its setup. The values they submit are
# ordinary test data, never credentials: TEST_PASSWORD is a local literal
# (IMPLEMENTER RULES, rule 6).

# Satisfies SPEC-2's password policy and is long enough to be obviously a test
# value. Tests that commit (SPEC-3 test 33) use their own e-mail addresses, so a
# leftover row can never collide with a rolled-back one.
TEST_PASSWORD = "spec-3 test password"


def _future_start(offset: timedelta) -> datetime:
    """Return the UTC instant ``offset`` after now (SPEC-3 Section 5).

    Every SPEC-3 test builds its dates and instants this way rather than from a
    hard-coded calendar date, so the suite keeps working on whatever day it is
    run. A non-positive ``offset`` yields an instant that is not in the future,
    which the API rejects with ``SHOWTIME_IN_PAST`` (FR-10).

    Args:
        offset: How far ahead of now the instant must be.

    Returns:
        datetime: ``datetime.now(UTC) + offset``, timezone-aware and in UTC.
    """
    return datetime.now(UTC) + offset


def _cinema_zone() -> ZoneInfo:
    """Return the configured calendar-day timezone (SPEC-3 FR-13).

    Returns:
        ZoneInfo: The zone named by ``CINEMA_TIMEZONE``.
    """
    return ZoneInfo(get_settings().cinema_timezone)


def _local_day(zone: ZoneInfo, offset_days: int) -> date:
    """Return the local calendar day ``offset_days`` from today.

    Args:
        zone: The cinema's timezone.
        offset_days: How many days ahead of the local today to look.

    Returns:
        date: The local calendar date.
    """
    return (datetime.now(UTC).astimezone(zone) + timedelta(days=offset_days)).date()


def _local_instant(
    zone: ZoneInfo,
    day: date,
    hour: int,
    minute: int,
    second: int = 0,
) -> datetime:
    """Build the UTC instant of a local wall-clock time on a local day.

    The seconds default to zero, so the common "on the hour" call keeps its
    three-argument form while a boundary test can ask for ``23:59:59``.

    Args:
        zone: The cinema's timezone.
        day: The local calendar day.
        hour: Local hour, 0..23.
        minute: Local minute, 0..59.
        second: Local second, 0..59; defaults to 0.

    Returns:
        datetime: The same moment expressed in UTC, timezone-aware.
    """
    return datetime.combine(day, time(hour, minute, second), tzinfo=zone).astimezone(
        UTC
    )


async def _admin_headers(
    client: httpx.AsyncClient,
    conn: AsyncConnection,
    email: str,
) -> dict[str, str]:
    """Sign an admin up, promote them in the database and return their header.

    Promotion happens directly on the caller's connection
    (:func:`_make_admin`) rather than through ``POST /admin/users/{id}/promote``,
    which would need an already-seeded administrator to authorize it. With the
    rolled-back connection the role change belongs to the test transaction, so it
    leaves no trace behind.

    Args:
        client: The ASGI client bound to the application under test.
        conn: The open connection the test shares with the application.
        email: The administrator's address. Each test should use its own, so a
            test that commits (SPEC-3 test 33) cannot collide with another.

    Returns:
        dict[str, str]: The ``Authorization`` header of the promoted admin.

    Raises:
        AssertionError: If the signup is not answered with 201 or the login is
            not answered with 200, with the response body attached.
    """
    response = await _create_user(client, email, TEST_PASSWORD)
    assert response.status_code == 201, response.text
    await _make_admin(conn, int(response.json()["id"]))
    return _auth_header(await _login(client, email, TEST_PASSWORD))


async def _create_movie(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    title: str,
    genre_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Create a movie through ``POST /admin/movies`` (SPEC-3 FR-3).

    Only the two fields the SPEC-3 tests vary are exposed; a test that needs a
    description or a poster URL posts directly, so its payload stays visible.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting administrator's ``Authorization`` header.
        title: The movie title.
        genre_ids: Genre ids to link, or ``None`` to omit the field entirely
            (which the schema treats the same as an empty list, FR-3).

    Returns:
        dict[str, Any]: The 201 response body, i.e. a ``MovieOut``.

    Raises:
        AssertionError: If the movie is not created, with the body attached.
    """
    payload: dict[str, Any] = {"title": title}
    if genre_ids is not None:
        payload["genre_ids"] = genre_ids
    response = await client.post("/admin/movies", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _create_screen(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    name: str,
    rows: int,
    seats_per_row: int,
) -> dict[str, Any]:
    """Create a screen through ``POST /admin/screens`` (SPEC-3 FR-9).

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting administrator's ``Authorization`` header.
        name: The screen name, which must be unique.
        rows: Number of seat rows; the API bounds it to 1..26.
        seats_per_row: Seats in each row; the API bounds it to 1..40.

    Returns:
        dict[str, Any]: The 201 response body, i.e. a ``ScreenOut``.

    Raises:
        AssertionError: If the screen is not created, with the body attached.
    """
    response = await client.post(
        "/admin/screens",
        json={"name": name, "rows": rows, "seats_per_row": seats_per_row},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _create_showtime(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    movie_id: int,
    screen_id: int,
    starts_at: datetime,
    price_cents: int,
) -> dict[str, Any]:
    """Create a showtime through ``POST /admin/showtimes`` (SPEC-3 FR-10).

    ``starts_at`` is sent with ``isoformat()``, so a UTC instant is submitted as
    ``+00:00`` and a value built in another zone keeps its own offset -- the API
    normalises both to the same instant (FR-15).

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting administrator's ``Authorization`` header.
        movie_id: The id of an existing movie.
        screen_id: The id of an existing screen.
        starts_at: A timezone-aware start instant, strictly in the future.
        price_cents: Ticket price in integer cents, 1..100 000 000.

    Returns:
        dict[str, Any]: The 201 response body, i.e. a ``ShowtimeOut``.

    Raises:
        AssertionError: If the showtime is not created, with the body attached.
    """
    response = await client.post(
        "/admin/showtimes",
        json={
            "movie_id": movie_id,
            "screen_id": screen_id,
            "starts_at": starts_at.isoformat(),
            "price_cents": price_cents,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


_FutureStart = Callable[[timedelta], datetime]
_AdminHeaders = Callable[
    [httpx.AsyncClient, AsyncConnection, str], Awaitable[dict[str, str]]
]


class _CreateMovie(Protocol):
    """The :func:`_create_movie` signature, with ``genre_ids`` optional.

    A protocol rather than a ``Callable`` alias because an alias cannot express
    the defaulted fourth argument, and the tests call the helper both ways.
    """

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


@pytest.fixture
def future_start() -> _FutureStart:
    """Expose :func:`_future_start` as ``future_start(offset) -> datetime``.

    Returns:
        _FutureStart: The instant builder.
    """
    return _future_start


@pytest.fixture
def admin_headers() -> _AdminHeaders:
    """Expose :func:`_admin_headers` as ``admin_headers(client, conn, email)``.

    Returns:
        _AdminHeaders: The acting-administrator helper.
    """
    return _admin_headers


@pytest.fixture
def create_movie_via_api() -> _CreateMovie:
    """Expose :func:`_create_movie` as ``create_movie_via_api(...)``.

    Returns:
        _CreateMovie: The movie helper.
    """
    return _create_movie


@pytest.fixture
def create_screen_via_api() -> _CreateScreen:
    """Expose :func:`_create_screen` as ``create_screen_via_api(...)``.

    Returns:
        _CreateScreen: The screen helper.
    """
    return _create_screen


@pytest.fixture
def create_showtime_via_api() -> _CreateShowtime:
    """Expose :func:`_create_showtime` as ``create_showtime_via_api(...)``.

    Returns:
        _CreateShowtime: The showtime helper.
    """
    return _create_showtime


# --- SPEC-4 / SPEC-5 reservation helpers ----------------------------------
#
# These builders were local to tests/integration/test_reservations_api.py
# until the SPEC-5 lifecycle tests needed the same setup, so they live in this
# file once instead of in two test modules. Every helper asserts the status it
# requires and returns parsed data, so a test that only cares about its final
# request never re-checks its setup. The constants they share -- the two URL
# templates, the unknown id and the SQL status literals -- are declared here
# too, derived from the vocabulary in src/core/enums.py.

_RESERVATIONS_URL = "/reservations"
_SEAT_MAP_TEMPLATE = "/showtimes/{showtime_id}/seats"
_UNKNOWN_ID = 999_999_999

# Ordinary throwaway test data, never a credential (IMPLEMENTER RULES, rule 6).
_USER_PASSWORD = "spec-4 test password"


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
        AssertionError: If the body does not have the standard shape or either
            field is missing or empty.
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
    """Assert a failure response has ``status``, the expected code and shape.

    Args:
        response: The failure response.
        status: The expected HTTP status.
        code: The expected machine-readable error code.
    """
    assert response.status_code == status, response.text
    assert _error_payload(response)["code"] == code, response.text


def _parse_optional_instant(value: str | None) -> datetime | None:
    """Parse an optional ISO 8601 instant from a response body.

    Args:
        value: The serialised instant, or ``None``.

    Returns:
        datetime | None: The parsed timezone-aware instant, or ``None``.
    """
    return None if value is None else datetime.fromisoformat(value)


async def _count_rows(conn: AsyncConnection, table: Table) -> int:
    """Count every row of one table inside the test transaction.

    Args:
        conn: The test's connection, shared with the application's requests.
        table: The table to count.

    Returns:
        int: The number of rows.
    """
    return int(
        (await conn.execute(select(func.count()).select_from(table))).scalar_one()
    )


async def _reservation_row_counts(conn: AsyncConnection) -> tuple[int, int]:
    """Return the reservation and seat-reservation row counts together.

    Args:
        conn: The test's connection.

    Returns:
        tuple[int, int]: ``(reservations, seat_reservations)`` row counts.
    """
    return (
        await _count_rows(conn, reservations),
        await _count_rows(conn, seat_reservations),
    )


async def _count_user_reservations(conn: AsyncConnection, user_id: int) -> int:
    """Count the reservations belonging to one user.

    Args:
        conn: The test's connection.
        user_id: The user whose rows to count.

    Returns:
        int: The number of reservation rows owned by ``user_id``.
    """
    return int(
        (
            await conn.execute(
                select(func.count())
                .select_from(reservations)
                .where(reservations.c.user_id == user_id)
            )
        ).scalar_one()
    )


async def _create_account(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
    email: str,
) -> tuple[int, dict[str, str]]:
    """Sign an ordinary user up, log them in and return their identity.

    Args:
        client: The ASGI client bound to the application under test.
        create_user: The shared signup fixture.
        login: The shared login fixture.
        auth_header: The shared bearer-header fixture.
        email: A unique address for this test.

    Returns:
        tuple[int, dict[str, str]]: The user's id and ``Authorization`` header.

    Raises:
        AssertionError: If signup is not answered with 201, with the body
            attached.
    """
    response = await create_user(client, email, _USER_PASSWORD)
    assert response.status_code == 201, response.text
    user_id = int(_json_body(response)["id"])
    token = await login(client, email, _USER_PASSWORD)
    return user_id, auth_header(token)


async def _create_screening(
    client: httpx.AsyncClient,
    conn: AsyncConnection,
    admin_headers: _AdminHeaders,
    future_start: _FutureStart,
    create_movie: _CreateMovie,
    create_screen: _CreateScreen,
    create_showtime: _CreateShowtime,
    *,
    email: str,
    title: str,
    screen_name: str,
    rows: int,
    seats_per_row: int,
    price_cents: int = 1_000,
    start_offset: timedelta = timedelta(days=1),
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Create an administrator, a movie, a screen with seats and a showtime.

    Every test that needs a screening calls this once inside the test, so no
    test depends on seeded data. The administrator is created and promoted by
    the shared SPEC-3 helpers.

    Args:
        client: The ASGI client bound to the application under test.
        conn: The rolled-back connection shared with the application.
        admin_headers: The shared acting-administrator fixture.
        future_start: The shared ``now``-relative instant builder.
        create_movie: The shared movie fixture.
        create_screen: The shared screen fixture.
        create_showtime: The shared showtime fixture.
        email: The administrator's unique address.
        title: The movie title.
        screen_name: The unique screen name.
        rows: Number of seat rows.
        seats_per_row: Seats in each row.
        price_cents: Ticket price in integer cents.
        start_offset: How far ahead of now the showtime starts.

    Returns:
        tuple: The administrator's header, the movie, the screen and the
        showtime response bodies, in that order.
    """
    headers = await admin_headers(client, conn, email)
    movie = await create_movie(client, headers, title)
    screen = await create_screen(client, headers, screen_name, rows, seats_per_row)
    showtime = await create_showtime(
        client,
        headers,
        int(movie["id"]),
        int(screen["id"]),
        future_start(start_offset),
        price_cents,
    )
    return headers, movie, screen, showtime


async def _seat_map(client: httpx.AsyncClient, showtime_id: int) -> dict[str, Any]:
    """Fetch and assert the public seat map of a showtime.

    Args:
        client: The ASGI client bound to the application under test.
        showtime_id: The showtime whose map to read.

    Returns:
        dict[str, Any]: The 200 response body.

    Raises:
        AssertionError: If the request is not answered with 200, with the body
            attached.
    """
    response = await client.get(_SEAT_MAP_TEMPLATE.format(showtime_id=showtime_id))
    assert response.status_code == 200, response.text
    return _json_body(response)


async def _seat_ids(client: httpx.AsyncClient, showtime_id: int) -> list[int]:
    """Return the seat ids of a showtime's screen in map order.

    Args:
        client: The ASGI client bound to the application under test.
        showtime_id: The showtime whose seats to read.

    Returns:
        list[int]: Seat ids ordered by row label then seat number.
    """
    body = await _seat_map(client, showtime_id)
    return [int(seat["seat_id"]) for seat in body["seats"]]


def _states_by_seat(body: dict[str, Any]) -> dict[int, str]:
    """Map each seat id of a seat-map body to its derived state.

    Args:
        body: A ``SeatMapOut`` response body.

    Returns:
        dict[int, str]: ``seat_id -> available | held | taken``.
    """
    return {int(seat["seat_id"]): str(seat["state"]) for seat in body["seats"]}


async def _screen_seat_ids(conn: AsyncConnection, screen_id: int) -> list[int]:
    """Load a screen's seat ids directly, ordered by row and number.

    Used where no showtime exists for the screen (the other-screen seat test)
    or where the showtime cannot be created through the API (the started test).

    Args:
        conn: The test's connection.
        screen_id: The screen whose seats to load.

    Returns:
        list[int]: Seat ids ordered by row label then seat number.
    """
    rows = (
        await conn.execute(
            select(seats.c.id)
            .where(seats.c.screen_id == screen_id)
            .order_by(seats.c.row_label, seats.c.seat_number)
        )
    ).all()
    return [int(row.id) for row in rows]


async def _hold(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    showtime_id: int,
    seat_ids: list[int],
) -> httpx.Response:
    """Post a hold request and return its raw response.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting user's ``Authorization`` header.
        showtime_id: The showtime to hold seats on.
        seat_ids: The requested seat ids.

    Returns:
        httpx.Response: The unasserted response, so a failure test can inspect
        it.
    """
    return await client.post(
        _RESERVATIONS_URL,
        json={"showtime_id": showtime_id, "seat_ids": seat_ids},
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


async def _release(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    reservation_id: int,
) -> httpx.Response:
    """Post a release request and return its raw response.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting user's ``Authorization`` header.
        reservation_id: The reservation to release.

    Returns:
        httpx.Response: The unasserted response.
    """
    return await client.post(
        f"{_RESERVATIONS_URL}/{reservation_id}/release",
        headers=headers,
    )


async def _get_reservation(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    reservation_id: int,
) -> httpx.Response:
    """Get one reservation and return its raw response.

    Args:
        client: The ASGI client bound to the application under test.
        headers: The acting user's ``Authorization`` header.
        reservation_id: The reservation to read.

    Returns:
        httpx.Response: The unasserted response.
    """
    return await client.get(
        f"{_RESERVATIONS_URL}/{reservation_id}",
        headers=headers,
    )


async def _stored_reservation_status(conn: AsyncConnection, reservation_id: int) -> str:
    """Return the stored status of one reservation row.

    Args:
        conn: The test's connection.
        reservation_id: The reservation primary key.

    Returns:
        str: The stored status (not the derived effective status).
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
    """Return the stored statuses of a reservation's seat rows.

    Args:
        conn: The test's connection.
        reservation_id: The reservation primary key.

    Returns:
        list[str]: One stored status per seat row, ordered by seat id.
    """
    rows = (
        await conn.execute(
            select(seat_reservations.c.status)
            .where(seat_reservations.c.reservation_id == reservation_id)
            .order_by(seat_reservations.c.seat_id)
        )
    ).all()
    return [str(row.status) for row in rows]


async def _assert_stored_status(
    conn: AsyncConnection,
    reservation_id: int,
    status: str,
) -> None:
    """Assert a reservation and every one of its seat rows store ``status``.

    Args:
        conn: The test's connection.
        reservation_id: The reservation primary key.
        status: The expected stored status.
    """
    assert await _stored_reservation_status(conn, reservation_id) == status
    seat_statuses = await _stored_seat_statuses(conn, reservation_id)
    assert seat_statuses, reservation_id
    assert set(seat_statuses) == {status}, seat_statuses


async def _insert_reservation(
    conn: AsyncConnection,
    *,
    user_id: int,
    showtime_id: int,
    seat_ids: list[int],
    status: str,
    created_at: datetime,
    confirmed_at: datetime | None = None,
    cancelled_at: datetime | None = None,
) -> int:
    """Insert a reservation and its seat rows directly, bypassing the service.

    Used for states the API cannot create: a confirmed seat that predates the
    hold under test and a reservation on a started showtime (SPEC-4 Section 5,
    tests 16 and 26).

    Args:
        conn: The test's connection.
        user_id: The owner's primary key.
        showtime_id: The showtime the reservation belongs to.
        seat_ids: The seats to attach to the reservation.
        status: The status stored on both the reservation and every seat row.
        created_at: The stored creation instant.
        confirmed_at: The stored confirmation instant, or ``None``.
        cancelled_at: The stored cancellation instant, or ``None``.

    Returns:
        int: The inserted reservation's primary key.
    """
    reservation_id = int(
        (
            await conn.execute(
                insert(reservations)
                .values(
                    user_id=user_id,
                    showtime_id=showtime_id,
                    status=status,
                    created_at=created_at,
                    confirmed_at=confirmed_at,
                    cancelled_at=cancelled_at,
                )
                .returning(reservations.c.id)
            )
        ).scalar_one()
    )
    await conn.execute(
        insert(seat_reservations).values(
            [
                {
                    "reservation_id": reservation_id,
                    "showtime_id": showtime_id,
                    "seat_id": seat_id,
                    "status": status,
                }
                for seat_id in seat_ids
            ]
        )
    )
    return reservation_id


async def _insert_showtime(
    conn: AsyncConnection,
    *,
    movie_id: int,
    screen_id: int,
    starts_at: datetime,
    price_cents: int,
) -> int:
    """Insert a showtime directly, bypassing the future-only API rule.

    Args:
        conn: The test's connection.
        movie_id: An existing movie.
        screen_id: An existing screen.
        starts_at: The start instant, past or future.
        price_cents: Ticket price in integer cents.

    Returns:
        int: The inserted showtime's primary key.
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


async def _set_reservation_status(
    conn: AsyncConnection,
    reservation_id: int,
    status: str,
) -> None:
    """Rewrite a reservation and all of its seat rows to a stored status.

    Args:
        conn: The test's connection.
        reservation_id: The reservation primary key.
        status: The status to store on the parent and every child row.
    """
    await conn.execute(
        update(reservations)
        .where(reservations.c.id == reservation_id)
        .values(status=status)
    )
    await conn.execute(
        update(seat_reservations)
        .where(seat_reservations.c.reservation_id == reservation_id)
        .values(status=status)
    )


async def _set_reservation_created_at(
    conn: AsyncConnection,
    reservation_id: int,
    created_at: datetime,
) -> None:
    """Rewrite only a reservation's ``created_at`` to age a hold.

    Args:
        conn: The test's connection.
        reservation_id: The reservation primary key.
        created_at: The creation instant to store.
    """
    await conn.execute(
        update(reservations)
        .where(reservations.c.id == reservation_id)
        .values(created_at=created_at)
    )


async def _stored_reservation_fields(
    conn: AsyncConnection,
    reservation_id: int,
) -> tuple[str, datetime, datetime | None, datetime | None]:
    """Return the stored status and lifecycle instants of one reservation.

    Args:
        conn: The test's connection.
        reservation_id: The reservation primary key.

    Returns:
        tuple: ``(status, created_at, confirmed_at, cancelled_at)``.
    """
    row = (
        await conn.execute(
            select(
                reservations.c.status,
                reservations.c.created_at,
                reservations.c.confirmed_at,
                reservations.c.cancelled_at,
            ).where(reservations.c.id == reservation_id)
        )
    ).one()
    return (
        str(row.status),
        row.created_at,
        row.confirmed_at,
        row.cancelled_at,
    )


async def _assert_rows_unchanged(
    conn: AsyncConnection,
    before: tuple[int, int],
) -> None:
    """Assert no reservation or seat-reservation row was added or removed.

    Args:
        conn: The test's connection.
        before: The pair returned by :func:`_reservation_row_counts` before the
            failing request.
    """
    assert await _reservation_row_counts(conn) == before


async def _assert_status_consistent(conn: AsyncConnection) -> None:
    """Assert every reservation has the same status as all of its seat rows.

    Args:
        conn: The test's connection.
    """
    mismatches = (
        await conn.execute(
            select(reservations.c.id, seat_reservations.c.id)
            .select_from(
                reservations.join(
                    seat_reservations,
                    seat_reservations.c.reservation_id == reservations.c.id,
                )
            )
            .where(reservations.c.status != seat_reservations.c.status)
        )
    ).all()
    assert mismatches == [], mismatches


# The SQL status literals used by tests are taken from the shared vocabulary.
_HELD = DEFAULT_RESERVATION_STATUS
_CONFIRMED = RESERVATION_STATUSES[1]
_CANCELLED = RESERVATION_STATUSES[2]
_EXPIRED = RESERVATION_STATUSES[3]

assert _HELD == DEFAULT_RESERVATION_STATUS
assert set(ACTIVE_RESERVATION_STATUSES) == {_HELD, _CONFIRMED}
assert set(RESERVATION_STATUSES) == {_HELD, _CONFIRMED, _CANCELLED, _EXPIRED}
