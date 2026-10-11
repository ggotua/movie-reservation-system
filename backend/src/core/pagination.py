"""Shared pagination envelope and page-parameter dependency (SPEC-3 FR-14).

Movies and showtimes lists both return the same page envelope and accept the
same ``limit`` / ``offset`` query parameters, so both live here in ``core``
instead of being repeated in each module (SPEC-3 Section 10, DRY check;
Section 11.1 places the shared page model in ``core``).

:class:`Page` is the response envelope; :func:`get_page_params` is the FastAPI
dependency that parses and validates the query parameters. Out-of-range values
are rejected by FastAPI with its normal request-validation response (422) --
they are never clamped (SPEC-3 Section 2.3, FR-14).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel


class Page[T](BaseModel):
    """A page of results plus the window that produced it (SPEC-3 FR-14).

    Attributes:
        items: The page's rows, already ordered by the owning endpoint.
        total: Total number of rows matching the query, ignoring ``limit`` and
            ``offset``, so a client can compute the number of pages.
        limit: The maximum page size that was applied, echoed back.
        offset: The number of skipped rows, echoed back.
    """

    items: list[T]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class PageParams:
    """Validated ``limit`` / ``offset`` query parameters (SPEC-3 FR-14).

    A frozen value holder rather than a mutable object: the dependency builds
    one per request and nothing should change it afterwards. Instances are
    produced by :func:`get_page_params`.

    Attributes:
        limit: Maximum number of rows to return; 1..100 inclusive.
        offset: Number of rows to skip before the page starts; 0 or greater.
    """

    limit: int
    offset: int


def get_page_params(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    """Parse and validate the shared ``limit`` / ``offset`` query parameters.

    Intended for use as a FastAPI dependency
    (``Annotated[PageParams, Depends(get_page_params)]``). The bounds are
    declared on the parameters themselves, so FastAPI rejects an out-of-range
    value with the API's normal 422 response instead of this function clamping
    it (SPEC-3 FR-14).

    Args:
        limit: Page size; must be between 1 and 100 inclusive. Defaults to 20.
        offset: Number of rows to skip; must be 0 or greater. Defaults to 0.

    Returns:
        PageParams: A frozen value holding the validated ``limit`` and
        ``offset``.
    """

    return PageParams(limit=limit, offset=offset)
