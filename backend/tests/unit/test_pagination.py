"""Unit test for the shared page-parameter bounds (SPEC-3 FR-14).

No database is involved. Because the bounds are enforced by FastAPI while it
parses the query string, the dependency is exercised through a tiny ASGI app
driven by ``httpx.ASGITransport`` -- no ``TestClient`` and no real server. The
app has no database dependency, so nothing connects.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import Depends, FastAPI

from src.core.pagination import PageParams, get_page_params

# A throwaway app whose only route echoes the validated page parameters.
_page_app = FastAPI()


@_page_app.get("/page")
def _echo_page_params(
    params: Annotated[PageParams, Depends(get_page_params)],
) -> dict[str, int]:
    """Return the validated ``limit`` and ``offset`` for the request.

    Args:
        params: The page parameters resolved by :func:`get_page_params`.

    Returns:
        dict[str, int]: ``{"limit": ..., "offset": ...}``.
    """
    return {"limit": params.limit, "offset": params.offset}


async def test_page_params_bounds() -> None:
    """limit 0/101 and offset -1 are rejected (422); 1/100/0 are accepted."""
    transport = httpx.ASGITransport(app=_page_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Omitted parameters use the defaults: limit 20, offset 0.
        default = await client.get("/page")
        assert default.status_code == 200
        assert default.json() == {"limit": 20, "offset": 0}

        # Values on the accepted boundaries (1, 100 and offset 0) return 200.
        for query in ("limit=1&offset=0", "limit=100&offset=0"):
            accepted = await client.get(f"/page?{query}")
            assert accepted.status_code == 200, accepted.text

        # Out-of-range values return 422; they are never clamped.
        for query in ("limit=0", "limit=101", "offset=-1"):
            rejected = await client.get(f"/page?{query}")
            assert rejected.status_code == 422, rejected.text
