"""Alembic migration environment for the Movie Reservation System.

This module wires Alembic to the two project sources of truth:

* ``src.core.config.get_settings().database_url`` — the same ``DATABASE_URL``
  the application engine uses (``src/db/engine.py``), so migrations always run
  against the configured database and no URL is duplicated in ``alembic.ini``.
* ``src.db.tables.metadata`` — the single :class:`~sqlalchemy.MetaData` holding
  every ``Table``. It is exposed as Alembic's ``target_metadata`` so a future
  ``alembic revision --autogenerate`` compares against the real schema.

Both offline (``alembic upgrade head --sql``) and online modes are supported.
The URL uses the psycopg (v3) driver. SQLAlchemy picks sync vs. async from the
engine factory, so the one ``DATABASE_URL`` value is reused unchanged: the
application builds an async engine from it, and this environment builds a
synchronous one.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import Connection, create_engine, pool

# Make `src` importable regardless of the directory alembic is invoked from.
# The project convention is to run from backend/, but resolving the backend
# directory from this file keeps the environment working from the repo root
# too (and does not depend on alembic.ini's prepend_sys_path value).
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from src.core.config import get_settings  # noqa: E402
from src.db.tables import metadata as target_metadata  # noqa: E402

# The Alembic Config object, providing access to values in alembic.ini.
config = context.config

# Configure Python logging from alembic.ini when the file is present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    """Return the configured database URL for the migration connection.

    Returns:
        str: ``Settings.database_url`` — the async psycopg URL from the
        environment. The ``postgresql+psycopg`` dialect serves both the
        synchronous engine built here and the application's async engine.

    Raises:
        pydantic.ValidationError: If ``DATABASE_URL`` (or another required
            setting such as ``JWT_SECRET``) is unset — the same fail-loud
            behaviour the application has at startup.
    """
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Run migrations in "offline" mode.

    Emits SQL to stdout (``alembic upgrade head --sql``) without connecting to
    a database, using the URL from configuration. This is the path used to
    review a migration's generated DDL in CI or before applying it.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    """Configure the context for ``connection`` and run the migrations.

    Args:
        connection: An open SQLAlchemy connection to migrate against. Pulled
            out of :func:`run_migrations_online` so the connection's lifetime
            is managed by a ``with`` block at the call site.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in "online" mode against a live database connection.

    A single connection is opened with a :class:`~sqlalchemy.pool.NullPool`
    (one connection, closed at the end — migrations do not want a pooled
    connection held open) and handed to :func:`_run_with_connection`.
    """
    engine = create_engine(_database_url(), poolclass=pool.NullPool)

    with engine.connect() as connection:
        _run_with_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
