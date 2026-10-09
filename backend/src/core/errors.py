"""Shared error type and body builder for the API (SPEC-2 Section 2.9).

Every error a route returns uses the shape fixed in
``docs/steering/conventions.md``::

    {"error": {"code": "SOME_CODE", "message": "Human-readable text."}}

:class:`ApiError` lets business logic raise a typed failure without importing
FastAPI; ``main.py`` installs the FastAPI exception handler that turns it into
a response. :func:`error_body` builds that JSON body and is reused by the
handler for FastAPI's own request-validation errors, so the shape is defined
in exactly one place (SPEC-2 DRY check).

This module deliberately imports nothing from the project and nothing from
FastAPI: ``core`` sits at the bottom of the dependency graph
(``docs/specs/SPEC-2-auth-and-roles.md`` Section 11.1).
"""

from __future__ import annotations


class ApiError(Exception):
    """A business failure that maps to a specific HTTP status and error code.

    Raising this from a module function lets the route layer stay thin (it
    does not build error responses itself) while keeping the failure typed and
    testable without a live request.

    Attributes:
        code: Stable, machine-readable ``SCREAMING_SNAKE_CASE`` code that
            clients branch on, e.g. ``INVALID_CREDENTIALS``.
        message: Human-readable text safe to show to a user.
        status_code: HTTP status that communicates the error category.
        headers: Optional response headers, e.g.
            ``{"WWW-Authenticate": "Bearer"}`` on a 401. ``None`` means no
            extra headers.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            code: The machine-readable error code.
            message: The human-readable error message.
            status_code: The HTTP status to answer with.
            headers: Optional extra response headers; defaults to ``None``.
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.headers = headers


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    """Build the standard error body for the API.

    Args:
        code: The stable, machine-readable error code.
        message: The human-readable error message.

    Returns:
        dict[str, dict[str, str]]: ``{"error": {"code": ..., "message": ...}}``
        as required by ``docs/steering/conventions.md``.
    """
    return {"error": {"code": code, "message": message}}
