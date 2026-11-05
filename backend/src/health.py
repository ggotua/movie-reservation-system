"""Unauthenticated liveness endpoint used by the deployment (SPEC-7B D6, FR-1).

The route exists for Render's health check and for the keep-awake ping
(SPEC-7B D6, D7), so it is deliberately the cheapest route in the application:
the handler declares no database dependency, no authentication dependency and
reads no setting. A database that is unreachable or asleep, a missing or expired
token and an invalid configuration value therefore cannot turn it into a
failure; that the process answered at all is exactly what a health check has to
measure.

It is a module of its own, mounted by :func:`src.main.create_app`, because every
other router belongs to one business module (``src/auth``, ``src/movies``,
``src/showtimes``, ``src/reservations``, ``src/reporting``) and this one belongs
to none. Nothing else about the API changes: every failure still gets the single
error shape produced by the handlers in ``main.py`` (SPEC-2 Section 2.9,
SPEC-7B FR-7).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

# No prefix and one documentation tag, so ``/docs`` groups the health check
# apart from the business routes (SPEC-7B smoke check S2).
router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    """Body of a successful ``GET /health`` response (SPEC-7B FR-1).

    Attributes:
        status: Always ``"ok"``. The field is declared instead of returning a
            bare string, so the response body is the object SPEC-7B D6 fixes
            and appears with that shape in the generated documentation.
    """

    status: Literal["ok"] = "ok"


@router.get("/health", response_model=HealthOut)
async def get_health() -> HealthOut:
    """Report that the HTTP service is answering (SPEC-7B D6, FR-1).

    Returns:
        HealthOut: The fixed body ``{"status": "ok"}``, which FastAPI sends with
        status 200. Nothing is read, opened or validated on the way.
    """
    return HealthOut()
