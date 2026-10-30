"""Integration tests for the CORS middleware (SPEC-7 Section 5.1, tests 2-4).

Every test drives the real application through an ASGI client
(``httpx.ASGITransport``) using the shared ``client`` fixture, so no server is
started. Tests 2 and 3 only exercise the browser handshake, which Starlette
answers before any route runs; test 4 needs no stored data either, because the
unauthenticated ``GET /auth/me`` fails before the database is read.

Test names match SPEC-7 Section 5.1 exactly, and each test names the FR it
verifies (SPEC-7 Section 6).
"""

from __future__ import annotations

from typing import Any

import httpx

from src.core.config import get_settings

# The endpoint used for the simple-request checks. It answers 401 without a
# token, so it exercises the error path without touching stored data.
_ME_URL = "/auth/me"

# The login route is only ever asked for a preflight; no request reaches it.
_LOGIN_URL = "/auth/login"

# Methods D2 allows, in the order main.py configures them.
_ALLOWED_METHODS = {"GET", "POST", "PATCH", "DELETE", "OPTIONS"}

# A well-formed origin that is not in the configured list. The port is unusual
# so it cannot coincide with the Vite defaults or a deployment value.
_DISALLOWED_ORIGIN = "http://not-configured.example:4444"


def _configured_origin() -> str:
    """Return the first origin the running application allows (FR-9).

    Reading it from the settings keeps the tests independent of the value in
    ``backend/.env`` or the environment, exactly as the application reads it.

    Returns:
        str: An origin the CORS middleware must accept.
    """
    return get_settings().cors_origins[0]


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Return a response body as a JSON object.

    Args:
        response: The response to read.

    Returns:
        dict[str, Any]: The decoded body.
    """
    body: dict[str, Any] = response.json()
    return body


async def test_cors_preflight_allows_configured_origin_and_authorization_header(
    client: httpx.AsyncClient,
) -> None:
    """A preflight from a configured origin is allowed with the API's scope (FR-9).

    The browser handshake must accept the bearer header the frontend sends and
    every method the API uses, and must not claim credentials are allowed.
    """
    origin = _configured_origin()
    response = await client.options(
        _LOGIN_URL,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == origin
    assert "vary" in response.headers

    methods = {
        method.strip()
        for method in response.headers["access-control-allow-methods"].split(",")
    }
    assert methods == _ALLOWED_METHODS

    allowed_headers = {
        header.strip().lower()
        for header in response.headers["access-control-allow-headers"].split(",")
    }
    assert {"authorization", "content-type"} <= allowed_headers

    # No cookies are involved (D2), so the browser must not be told otherwise.
    assert "access-control-allow-credentials" not in response.headers


async def test_cors_disallowed_origin_gets_no_allow_origin_header(
    client: httpx.AsyncClient,
) -> None:
    """An origin outside ``CORS_ORIGINS`` is never granted access (FR-9).

    Both the preflight handshake and an ordinary request are checked: the
    middleware adds no allow-origin header, so the browser blocks the response.
    """
    preflight = await client.options(
        _LOGIN_URL,
        headers={
            "Origin": _DISALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    simple = await client.get(_ME_URL, headers={"Origin": _DISALLOWED_ORIGIN})

    assert "access-control-allow-origin" not in preflight.headers
    assert "access-control-allow-origin" not in simple.headers


async def test_cors_headers_present_on_error_responses_and_error_shape_unchanged(
    client: httpx.AsyncClient,
) -> None:
    """A failing request from an allowed origin keeps the standard error body (FR-9).

    ``GET /auth/me`` without a token is answered by the API's own error handler,
    so this proves the CORS middleware wraps that response: the allow-origin
    header is present and the body is byte-for-byte the same as the body of the
    identical request sent without an ``Origin`` header.
    """
    origin = _configured_origin()
    without_origin = await client.get(_ME_URL)
    with_origin = await client.get(_ME_URL, headers={"Origin": origin})

    assert with_origin.status_code == 401, with_origin.text
    assert with_origin.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-origin" not in without_origin.headers

    # The standard error shape (SPEC-2 Section 2.9) is unchanged by CORS.
    assert with_origin.json() == without_origin.json()
    body = _json_object(with_origin)
    assert set(body) == {"error"}, body
    error: dict[str, str] = body["error"]
    assert set(error) == {"code", "message"}, error
    assert error["code"] == "UNAUTHENTICATED"
    assert error["message"]
    assert with_origin.headers["WWW-Authenticate"] == "Bearer"
