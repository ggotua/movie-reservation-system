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
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, update
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from src.core.config import get_settings
from src.core.enums import ADMIN_USER_ROLE
from src.db.tables import users

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
