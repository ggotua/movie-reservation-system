"""Unit test for ListParams and the pagination it reuses (SPEC-5 Section 5, test 2).

No database is involved. ``status`` and ``upcoming`` are validated directly on
the Pydantic model; ``limit``/``offset`` are validated by the shared
``get_page_params`` dependency through a tiny ASGI app (the same technique as
``test_pagination.py``), which proves ``ListParams`` reuses the one pagination
implementation instead of declaring its own bounds.
"""

from __future__ import annotations

from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from pydantic import ValidationError

from src.core.enums import ReservationStatus
from src.core.pagination import PageParams, get_page_params
from src.reservations.schemas import ListParams

# The default page window (SPEC-3 / SPEC-5 D4: limit 20, offset 0).
_PAGE = PageParams(limit=20, offset=0)

# A throwaway app whose only route echoes the validated page parameters. It has
# no database dependency, so nothing connects.
_page_app = FastAPI()


@_page_app.get("/page")
def _echo_page_params(
    params: Annotated[PageParams, Depends(get_page_params)],
) -> dict[str, int]:
    """Return the validated ``limit`` and ``offset`` for the request.

    Args:
        params: The page parameters resolved by :func:`get_page_params`, the
            same dependency ``ListParams`` reuses for its pagination window.

    Returns:
        dict[str, int]: ``{"limit": ..., "offset": ...}``.
    """
    return {"limit": params.limit, "offset": params.offset}


async def test_list_params_validation() -> None:
    """Test 2: status/upcoming accepted and rejected; limit/offset bounds hold."""
    # Every one of the four statuses is accepted, and omitting it is valid.
    valid_statuses: tuple[ReservationStatus, ...] = (
        "held",
        "confirmed",
        "cancelled",
        "expired",
    )
    for status in valid_statuses:
        params = ListParams(status=status, page=_PAGE)
        assert params.status == status
        assert params.upcoming is None
    assert ListParams(page=_PAGE).status is None

    # Anything outside the shared vocabulary is rejected.
    invalid_statuses: list[object] = ["pending", "HELD", "", 1]
    for invalid_status in invalid_statuses:
        with pytest.raises(ValidationError):
            ListParams.model_validate({"status": invalid_status, "page": _PAGE})

    # upcoming is a tri-state filter: omitted, true and false; query strings are
    # parsed into booleans and anything else is rejected.
    assert ListParams(page=_PAGE).upcoming is None
    assert ListParams(upcoming=True, page=_PAGE).upcoming is True
    assert ListParams(upcoming=False, page=_PAGE).upcoming is False
    assert (
        ListParams.model_validate({"upcoming": "true", "page": _PAGE}).upcoming is True
    )
    assert (
        ListParams.model_validate({"upcoming": "false", "page": _PAGE}).upcoming
        is False
    )
    invalid_upcoming_values: list[object] = ["maybe", 2, ""]
    for invalid_upcoming in invalid_upcoming_values:
        with pytest.raises(ValidationError):
            ListParams.model_validate({"upcoming": invalid_upcoming, "page": _PAGE})

    # limit/offset come from the one shared pagination implementation.
    transport = httpx.ASGITransport(app=_page_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Omitted parameters use the shared defaults: limit 20, offset 0.
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
