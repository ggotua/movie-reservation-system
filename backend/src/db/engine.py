"""Async SQLAlchemy engine and session factory.

The engine is created once per process from ``Settings.database_url``.
Business modules should receive sessions through :func:`get_session` (a
FastAPI dependency) rather than importing the engine directly, so tests can
substitute a different session source.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
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
