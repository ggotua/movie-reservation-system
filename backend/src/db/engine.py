"""Async SQLAlchemy engine and session factory.

The engine is created once per process from ``Settings.database_url``.
Business modules should receive sessions through :func:`get_session` (a
FastAPI dependency) rather than importing the engine directly, so tests can
substitute a different session source.

The transaction boundary for the API is :func:`get_connection`; routes and
dependencies take it through the :data:`DbConnection` alias, so the
``scope="function"`` option is written in exactly one place (SPEC-2 Sections
2.7 and 11.1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import get_settings


def build_engine(database_url: str) -> AsyncEngine:
    """Build a new async engine for ``database_url``.

    Args:
        database_url: A SQLAlchemy async URL, e.g.
            ``postgresql+psycopg://user:pass@host:5432/dbname``.

    Returns:
        AsyncEngine: A configured async engine. ``pool_pre_ping`` is enabled
        so stale connections are recycled after the database restarts (common
        in local Docker workflows).
    """
    return create_async_engine(database_url, pool_pre_ping=True)


def build_session_factory(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Wrap ``async_engine`` in a session factory.

    Args:
        async_engine: The engine produced by :func:`build_engine`.

    Returns:
        async_sessionmaker[AsyncSession]: A callable factory that creates
        sessions bound to ``async_engine``. ``expire_on_commit`` is disabled
        so row values remain readable after a commit without an extra refresh
        round-trip.
    """
    return async_sessionmaker(async_engine, expire_on_commit=False)


# Module-level singletons, built from validated config at import time. Import
# of this module therefore fails loudly when required settings are missing.
engine: AsyncEngine = build_engine(get_settings().database_url)
async_session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a database session for the lifetime of a request.

    Intended for use as a FastAPI dependency (``Depends(get_session)``). The
    session is closed when the request scope exits, including on error.

    Yields:
        AsyncSession: A new session bound to the module-level engine.
    """
    async with async_session_factory() as session:
        yield session


async def get_connection() -> AsyncIterator[AsyncConnection]:
    """Yield a database connection wrapped in one request-scoped transaction.

    Intended for use as a FastAPI dependency. ``engine.begin()`` starts a
    transaction and commits it when the route and all its dependencies returned
    normally, and rolls back when an exception reaches this generator
    (SPEC-2 Section 2.7). Exceptions are deliberately not caught here, so the
    rollback and the API's error handling both stay in FastAPI's hands.

    Commit timing (important): FastAPI runs a yield-dependency's exit code
    *after* the response has been sent unless the dependency is declared with
    ``scope="function"``. The commit must be visible before the client receives
    the response, otherwise a follow-up request can miss the data this request
    just wrote. Callers must therefore never depend on this function directly:
    they annotate the parameter as :data:`DbConnection`, the one alias below
    that supplies ``scope="function"``. FastAPI 0.142.2 supports that keyword;
    without it the default ``scope="request"`` applies and the commit happens
    only after the response has gone out.

    Yields:
        AsyncConnection: A connection bound to a transaction that is committed
        on success and rolled back on error.

    Side effects:
        Opens one pooled connection and one transaction per request, held until
        the dependency's exit code runs.
    """
    async with engine.begin() as connection:
        yield connection


# The one way the application obtains a request-scoped database connection.
#
# ``scope="function"`` moves this generator's exit code -- the COMMIT -- into
# the exit stack that FastAPI closes BEFORE it sends the response. With the
# default ``scope="request"`` the commit would run only after the response had
# gone out, so a client's follow-up request could miss the row this request just
# wrote. Declaring it through one alias keeps that option written exactly once
# (SPEC-2 Section 11.1): every route and dependency that annotates a parameter
# as ``DbConnection`` resolves to the same callable, so FastAPI's dependency
# cache hands the whole request ONE connection and ONE transaction.
#
# Only a coroutine, or another ``scope="function"`` dependency, may depend on
# this alias: FastAPI raises ``DependencyScopeError`` when a generator that
# resolves at ``scope="request"`` depends on a ``scope="function"`` one. That is
# why ``get_current_user`` and ``require_admin`` are plain coroutines.
DbConnection = Annotated[AsyncConnection, Depends(get_connection, scope="function")]
