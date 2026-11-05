"""Integration test for the deployment health endpoint (SPEC-7B Section 5.1, test 1).

The test drives the real application through an ASGI client
(``httpx.ASGITransport``), so no server and no port are involved, and it builds
the application itself instead of using the shared ``client`` fixture: that
fixture hands every request a connection to the real test database, which is
exactly what this test must prove is never touched. The application's database
dependency is overridden with a generator that raises, so a health route that
resolved it would answer 500 (SPEC-7B D6, FR-1).

Test name matches SPEC-7B Section 5.1 exactly, and the test names the FR it
verifies (SPEC-7B Section 4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncConnection

from src.db.engine import get_connection
from src.main import create_app

# The route under test and the body SPEC-7B D6 fixes.
_HEALTH_URL = "/health"
_EXPECTED_BODY: dict[str, str] = {"status": "ok"}

# Same base URL as the shared ASGI client in ``tests/conftest.py``; nothing is
# sent over a socket, so its value is never resolved.
_BASE_URL = "http://test"


async def _failing_database() -> AsyncIterator[AsyncConnection]:
    """Stand in for ``get_connection`` and fail immediately (SPEC-7B FR-1).

    Yields:
        AsyncConnection: Never yields: the first step of the generator raises.

    Raises:
        RuntimeError: Always, so any request that depends on the database fails
            loudly instead of reaching one. The trailing ``yield`` is
            unreachable and exists only to make this an async generator, the
            shape FastAPI's dependency resolution expects.
    """
    raise RuntimeError("the health endpoint must not use the database dependency")
    yield  # pragma: no cover


def _app_with_failing_database() -> FastAPI:
    """Build the real application with its database dependency overridden.

    Returns:
        FastAPI: A fresh application carrying the real routers and error
        handlers, whose ``get_connection`` raises whenever a route asks for it.
    """
    app = create_app()
    app.dependency_overrides[get_connection] = _failing_database
    return app


async def test_health_returns_ok_without_database() -> None:
    """``GET /health`` answers 200 and the exact body with the database down (FR-1).

    The database dependency is overridden with :func:`_failing_database`, so this
    response can only be produced by a route that never opens a connection. The
    body must be exactly ``{"status": "ok"}`` -- no extra field, no envelope --
    because an external monitor (SPEC-7B D7) reads it.
    """
    app = _app_with_failing_database()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=_BASE_URL,
    ) as client:
        response = await client.get(_HEALTH_URL)

    assert response.status_code == 200, response.text
    body: Any = response.json()
    assert body == _EXPECTED_BODY, body
