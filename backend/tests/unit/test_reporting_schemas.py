"""Unit test for the reporting query-parameter models (SPEC-6 Section 5, test 2).

No database is involved. ``group_by``, ``status`` and the dates are validated
directly on the Pydantic models; ``limit``/``offset`` are validated by the
shared ``get_page_params`` dependency through a tiny ASGI app (the same
technique as ``test_list_params.py``), which proves the reporting models reuse
the one pagination implementation instead of declaring their own bounds (FR-9).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from pydantic import ValidationError

from src.core.enums import ReservationStatus
from src.core.pagination import PageParams, get_page_params
from src.reporting.schemas import (
    AdminReservationListParams,
    CapacityListParams,
    RevenueParams,
)

# The default page window (SPEC-3 / SPEC-6 Section 2.3: limit 20, offset 0).
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
            same dependency the reporting query models reuse.

    Returns:
        dict[str, int]: ``{"limit": ..., "offset": ...}``.
    """
    return {"limit": params.limit, "offset": params.offset}


async def test_report_params_validation() -> None:
    """Test 2: group_by/status accepted and rejected; limit/offset bounds hold."""
    # group_by accepts exactly the two groupings, and defaults to movie (2.3).
    assert RevenueParams().group_by == "movie"
    for group_by in ("movie", "day"):
        assert RevenueParams.model_validate({"group_by": group_by}).group_by == group_by
    for invalid_group_by in ("week", "Movie", "DAY", "", 1):
        with pytest.raises(ValidationError):
            RevenueParams.model_validate({"group_by": invalid_group_by})

    # status accepts the four effective statuses of the shared vocabulary and
    # rejects anything else: an unknown value is a shape error, answered with
    # 422 VALIDATION_ERROR, never with INVALID_DATE_RANGE.
    valid_statuses: tuple[ReservationStatus, ...] = (
        "held",
        "confirmed",
        "cancelled",
        "expired",
    )
    for status in valid_statuses:
        params = AdminReservationListParams(status=status, page=_PAGE)
        assert params.status == status
    assert AdminReservationListParams(page=_PAGE).status is None
    for invalid_status in ("pending", "HELD", "", 1):
        with pytest.raises(ValidationError):
            AdminReservationListParams.model_validate(
                {"status": invalid_status, "page": _PAGE}
            )

    # Dates are calendar days: an ISO string parses, a non-existent day does
    # not, and omitting a side leaves it unbounded (FR-3 decides the ordering).
    capacity = CapacityListParams.model_validate(
        {"date_from": "2026-10-12", "date_to": "2026-10-14", "page": _PAGE}
    )
    assert capacity.date_from == date(2026, 10, 12)
    assert capacity.date_to == date(2026, 10, 14)
    assert CapacityListParams(page=_PAGE).date_from is None
    # Pydantic's parser rejects an impossible day and a non-date string, and
    # rejects a datetime that is not exactly midnight; an exact-midnight
    # datetime string is read as that calendar day, which is harmless.
    for invalid_date in ("2026-02-30", "2026-10-12T15:30:00", "12/10/2026", "x"):
        with pytest.raises(ValidationError):
            CapacityListParams.model_validate(
                {"date_from": invalid_date, "page": _PAGE}
            )
    midnight = CapacityListParams.model_validate(
        {"date_from": "2026-10-12T00:00:00", "page": _PAGE}
    )
    assert midnight.date_from == date(2026, 10, 12)

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
