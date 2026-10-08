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
A synchronous SQLAlchemy engine (``create_engine`` on the
``postgresql+psycopg://`` URL) plus plain synchronous fixtures. Async psycopg
fails on Windows under the default ``ProactorEventLoop``, and these tests only
prove database constraints, so async would add cost without adding coverage.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine, make_url

from src.core.config import get_settings

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
