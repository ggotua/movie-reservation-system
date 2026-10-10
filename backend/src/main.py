"""Application factory, global error handling and the server entry point.

This module is the assembly point of the API skeleton (SPEC-2 Section 2.1): it
builds the FastAPI application, mounts the auth and admin routers, and installs
the exception handlers that give every failure the single shape fixed in
``docs/steering/conventions.md`` (SPEC-2 Sections 2.1, 2.9; FR-12).

The database dependency is deliberately not wrapped or overridden here.
``get_connection`` opens one transaction per request and commits before the
response is sent, and it rolls back when the handler raises (SPEC-2 Section
2.7), so a failure handled below has already been rolled back.

Nothing else is added: no CORS (it arrives with the frontend, Section 9), no
extra routes and no middleware.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.auth.router import admin_router, auth_router
from src.core.errors import ApiError, error_body

logger = logging.getLogger(__name__)

_APP_TITLE = "Movie Reservation System"

# The generic 500 message is a constant because it must say nothing about the
# failure: the detail belongs in the log line, not in the response
# (SPEC-2 Section 2.9, Section 7).
_INTERNAL_ERROR_MESSAGE = "Unexpected error."

# Pydantic prefixes every message from a custom validator with this text.
# Removing it leaves the validator's own wording, which for this project never
# contains the submitted value.
_VALUE_ERROR_PREFIX = "Value error, "

# Starlette raises ``HTTPException`` for framework-level failures, an unknown
# route (404) and a wrong method (405) among them. Only a code and a fixed
# message are reported -- never ``exc.detail`` -- so no detail string a future
# handler might write can reach a client through this path.
_HTTP_EXCEPTION_REPLIES: dict[int, tuple[str, str]] = {
    404: ("NOT_FOUND", "Not found."),
    405: ("METHOD_NOT_ALLOWED", "Method not allowed."),
}
_HTTP_EXCEPTION_FALLBACK: tuple[str, str] = (
    "HTTP_ERROR",
    "The request could not be completed.",
)


def _validation_error_message(error: RequestValidationError) -> str:
    """Summarise the first request-validation problem for the client.

    Only the field path and Pydantic's own reason are used. The error's
    ``input`` and ``ctx`` entries and the location's leading ``body`` marker are
    all dropped, so a submitted password is never echoed back (SPEC-2
    Section 2.9).

    Args:
        error: The validation error FastAPI raised while parsing the request.

    Returns:
        str: ``"<field>: <reason>"`` describing the first problem, or a generic
        sentence when the error carries no details.
    """
    errors = error.errors()
    if not errors:
        return "Request validation failed."

    first = errors[0]
    path = [str(part) for part in first.get("loc", ()) if part != "body"]
    field = ".".join(path) if path else "body"
    reason = str(first.get("msg", "invalid value")).removeprefix(_VALUE_ERROR_PREFIX)
    return f"{field}: {reason}"


async def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    """Render an :class:`~src.core.errors.ApiError` (SPEC-2 FR-12).

    Args:
        request: The incoming request; unused, present because Starlette calls
            every handler with it.
        exc: The raised exception. Registered for ``ApiError`` only, so the
            narrowing guard below is unreachable and re-raises instead of
            guessing a status.

    Returns:
        JSONResponse: The error's own status code, the standard error body, and
        any headers it carried -- the ``WWW-Authenticate: Bearer`` challenge
        above all.
    """
    if not isinstance(exc, ApiError):
        raise exc
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message),
        headers=exc.headers,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Re-shape FastAPI's request-validation failure (SPEC-2 FR-3, FR-12).

    Args:
        request: The incoming request; unused.
        exc: The raised exception, registered as ``RequestValidationError``.

    Returns:
        JSONResponse: Status 422 with code ``VALIDATION_ERROR`` and a short
        summary of the first problem, never the submitted values.
    """
    if not isinstance(exc, RequestValidationError):
        raise exc
    return JSONResponse(
        status_code=422,
        content=error_body("VALIDATION_ERROR", _validation_error_message(exc)),
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Re-shape Starlette's framework-level HTTP errors (SPEC-2 FR-12).

    Args:
        request: The incoming request; unused.
        exc: The raised exception, registered as ``StarletteHTTPException``.
            FastAPI's own ``HTTPException`` is a subclass, so this covers it
            too.

    Returns:
        JSONResponse: ``NOT_FOUND`` (404), ``METHOD_NOT_ALLOWED`` (405) or the
        generic ``HTTP_ERROR`` code, always with the standard error body.
    """
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    code, message = _HTTP_EXCEPTION_REPLIES.get(
        exc.status_code, _HTTP_EXCEPTION_FALLBACK
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, message),
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Log any unexpected failure with its traceback and answer a bare 500.

    Args:
        request: The incoming request, used for the method and path of the log
            line only -- never the query string or the body, which may hold a
            credential.
        exc: The unhandled exception.

    Returns:
        JSONResponse: Status 500 with code ``INTERNAL_ERROR`` and a message that
        describes nothing about the failure (SPEC-2 Sections 2.9, 7).
    """
    logger.error(
        "unhandled_exception method=%s path=%s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content=error_body("INTERNAL_ERROR", _INTERNAL_ERROR_MESSAGE),
    )


def create_app() -> FastAPI:
    """Build the configured FastAPI application (SPEC-2 Section 2.1).

    Returns:
        FastAPI: The application with both routers mounted and the four
        exception handlers installed. A fresh instance is built on every call,
        so a test can build its own instead of importing the module-level
        :data:`app`.
    """
    app = FastAPI(title=_APP_TITLE)
    app.include_router(auth_router)
    app.include_router(admin_router)

    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_error)
    return app


# Module-level instance so ``uvicorn src.main:app`` works without an extra
# factory flag (SPEC-2 Section 2.8).
app = create_app()


def run() -> None:
    """Run the development server on ``127.0.0.1:8000`` (SPEC-2 Section 2.8).

    The supported local entry point is ``python -m src.main`` from ``backend/``.
    On Windows the server is started on an :class:`asyncio.SelectorEventLoop`,
    because async psycopg cannot run on the ``ProactorEventLoop`` that uvicorn
    would otherwise select there and every request needing the database would
    fail. Elsewhere the default event loop from the platform's policy is used.

    Returns:
        None: It runs until the server is stopped.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=8000)
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    run()
