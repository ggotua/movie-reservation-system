"""FastAPI dependencies that turn a request into a user or an admin.

:func:`get_current_user` and :func:`require_admin` are the two shared
authorization dependencies every later module reuses unchanged (SPEC-2
Section 2.5, FR-6, FR-7, FR-8). They translate HTTP credentials into a
:class:`~src.auth.service.UserRecord` and raise
:class:`~src.core.errors.ApiError`; the rules themselves stay in
:mod:`src.auth.service` (SPEC-2 Section 11).

Two constraints shape this module:

* The bearer scheme is built with ``auto_error=False``, so an absent or
  malformed ``Authorization`` header yields ``None`` instead of FastAPI's own
  error body. Every rejection then goes through :func:`_unauthenticated`, so
  all of them share one status, one error code and the
  ``WWW-Authenticate: Bearer`` header (SPEC-2 Sections 2.5, 2.9; FR-12).
* Both dependencies are plain coroutines, never generators. FastAPI raises
  ``DependencyScopeError`` when a generator that resolves at ``scope="request"``
  depends on the ``scope="function"`` :data:`~src.db.engine.DbConnection` alias,
  and a request-scoped generator would also move the database commit past the
  response (SPEC-2 Section 2.7).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.service import UserRecord, get_user_by_id
from src.core.config import get_settings
from src.core.enums import ADMIN_USER_ROLE
from src.core.errors import ApiError
from src.core.security import decode_access_token
from src.db.engine import DbConnection

logger = logging.getLogger(__name__)

# Declarative data, like the table definitions: one shared scheme instance keeps
# a single "HTTPBearer" security scheme in the generated OpenAPI document
# instead of one per dependency.
#
# ``auto_error=False`` is required: with the default ``True`` a missing header,
# a header without a token, or a non-bearer scheme makes FastAPI raise its own
# ``HTTPException`` with its own body, which would bypass the project's error
# shape (SPEC-2 Section 2.9).
bearer_scheme = HTTPBearer(auto_error=False)


def _unauthenticated() -> ApiError:
    """Build the single 401 returned for every failed authentication.

    Built fresh on each call rather than shared as a constant so the mutable
    ``headers`` mapping can never be changed by one response and observed by
    another.

    Returns:
        ApiError: ``UNAUTHENTICATED`` with status 401 and
        ``WWW-Authenticate: Bearer`` (SPEC-2 Sections 2.5, 2.9).
    """
    return ApiError(
        "UNAUTHENTICATED",
        "Authentication credentials were not provided or are invalid.",
        401,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    conn: DbConnection,
) -> UserRecord:
    """Resolve the authenticated user for a request (SPEC-2 FR-6, FR-7).

    The token is only ever used to identify the user; the account is then
    loaded from the database on every request, so the request carries the role
    stored *now* and a token can never hold a stale role (SPEC-2 Section 2.4).

    Every failure answers the same 401, so a client cannot tell a missing token
    from a bad one from a deleted account (SPEC-2 FR-7).

    Args:
        credentials: The parsed ``Authorization: Bearer`` header, or ``None``
            when the header is missing, carries no token, or uses another
            scheme (``bearer_scheme`` disables FastAPI's own rejection).
        conn: The request-scoped connection, shared with the route handler.

    Returns:
        UserRecord: The account named by the token's ``sub`` claim, with the
        role currently stored in the database.

    Raises:
        ApiError: ``UNAUTHENTICATED`` with status 401 when the header is
            absent or malformed, the token is invalid or expired, or the user
            it names no longer exists.
    """
    if credentials is None:
        logger.info("authentication_failed reason=missing_credentials")
        raise _unauthenticated()

    user_id = decode_access_token(credentials.credentials, get_settings().jwt_secret)
    if user_id is None:
        # The token itself is never logged, only the fact that it failed
        # (SPEC-2 Section 7).
        logger.info("authentication_failed reason=invalid_token")
        raise _unauthenticated()

    user = await get_user_by_id(conn, user_id)
    if user is None:
        logger.info("authentication_failed reason=unknown_user user_id=%s", user_id)
        raise _unauthenticated()

    return user


async def require_admin(
    user: Annotated[UserRecord, Depends(get_current_user)],
) -> UserRecord:
    """Require the authenticated user to hold the ``admin`` role (FR-8).

    This depends on :func:`get_current_user` rather than reading the token
    again, so authentication always happens before authorization: an
    unauthenticated caller is answered 401 by the inner dependency and never
    reaches the role check (SPEC-2 Section 2.5).

    Args:
        user: The authenticated user resolved by :func:`get_current_user`.

    Returns:
        UserRecord: The same user, unchanged, so a route can log or pass on the
        acting admin's id.

    Raises:
        ApiError: ``FORBIDDEN`` with status 403 when the stored role is not
            ``admin``.
    """
    if user.role != ADMIN_USER_ROLE:
        logger.info("authorization_failed reason=not_admin user_id=%s", user.id)
        raise ApiError(
            "FORBIDDEN",
            "This action requires administrator privileges.",
            403,
        )
    return user
