"""Thin HTTP handlers for the auth and admin endpoints (SPEC-2 Section 2.2).

Every handler only parses its inputs, calls the matching function in
:mod:`src.auth.service` and shapes the response; the rules themselves live in
the service layer (``docs/steering/conventions.md``). ``response_model`` is set
on every route, so responses are serialised from the declared schema and a
stray field -- a password hash above all -- can never reach the client
(SPEC-2 Section 2.2).

Connections are requested through :data:`~src.db.engine.DbConnection`. Because
:func:`~src.auth.dependencies.get_current_user` uses the same alias, FastAPI's
dependency cache resolves it once, so the route handler and the dependencies it
depends on share ONE connection and ONE transaction (SPEC-2 Sections 2.5, 2.7).

Routes and their dependencies:
    POST /auth/signup                     no auth; ``DbConnection``
    POST /auth/login                      no auth; ``DbConnection``
    GET  /auth/me                         ``get_current_user``
    POST /admin/users/{user_id}/promote   ``require_admin`` (and, through it,
                                          ``get_current_user``) + ``DbConnection``
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from src.auth.dependencies import get_current_user, require_admin
from src.auth.schemas import LoginRequest, SignupRequest, TokenOut, UserOut
from src.auth.service import UserRecord, authenticate, promote_to_admin, signup
from src.core.config import get_settings
from src.core.errors import ApiError
from src.core.security import create_access_token
from src.db.engine import DbConnection

# Two routers so the Swagger UI groups the public account endpoints separately
# from the admin-only ones, and so a later SPEC can attach admin authorization
# to a whole router at once.
auth_router = APIRouter(prefix="/auth", tags=["auth"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])

# Seconds in a minute, used to report a token's lifetime alongside the
# ``JWT_EXPIRY_MINUTES`` value it is derived from (SPEC-2 Section 2.2).
_SECONDS_PER_MINUTE = 60


@auth_router.post(
    "/signup",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
)
async def signup_user(payload: SignupRequest, conn: DbConnection) -> UserRecord:
    """Create an account and return it (SPEC-2 FR-1).

    Args:
        payload: The validated request body. The password policy and the email
            syntax were already enforced by :class:`SignupRequest`, and extra
            fields (``role`` above all) are rejected there too (FR-3).
        conn: The request-scoped connection; the service writes through it and
            the dependency commits before the response is sent.

    Returns:
        UserRecord: The stored account, with role ``user``. Serialised through
        ``UserOut``, which has no ``password_hash`` field.

    Raises:
        ApiError: Raised by :func:`src.auth.service.signup` as
            ``EMAIL_ALREADY_REGISTERED`` with status 409 when the normalized
            address is already taken (FR-2).
    """
    return await signup(conn, payload.email, payload.password)


@auth_router.post("/login", response_model=TokenOut)
async def login(payload: LoginRequest, conn: DbConnection) -> TokenOut:
    """Exchange credentials for a bearer token (SPEC-2 FR-4, FR-5).

    Args:
        payload: The validated request body. It applies no password policy, so
            a credential that would be rejected at signup still ends in the one
            401 below rather than a 422 (SPEC-2 Section 2.3).
        conn: The request-scoped connection; login only reads.

    Returns:
        TokenOut: A freshly signed token and its lifetime in seconds.

    Raises:
        ApiError: ``INVALID_CREDENTIALS`` with status 401, identical for an
            unknown email and for a wrong password (FR-5).
    """
    user = await authenticate(conn, payload.email, payload.password)
    if user is None:
        # authenticate() already logged the failure without the email and
        # without a user id, so this branch only builds the response.
        raise ApiError(
            "INVALID_CREDENTIALS",
            "Incorrect email or password.",
            401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = get_settings()
    return TokenOut(
        access_token=create_access_token(
            user.id,
            settings.jwt_secret,
            settings.jwt_expiry_minutes,
        ),
        token_type="bearer",
        expires_in=settings.jwt_expiry_minutes * _SECONDS_PER_MINUTE,
    )


@auth_router.get("/me", response_model=UserOut)
async def read_current_user(
    user: Annotated[UserRecord, Depends(get_current_user)],
) -> UserRecord:
    """Return the account the bearer token identifies (SPEC-2 FR-6).

    The connection is supplied by the ``get_current_user`` dependency, so this
    handler needs no database parameter of its own.

    Args:
        user: The authenticated user, re-read from the database for this
            request, so ``role`` is always the currently stored one.

    Returns:
        UserRecord: The current account, serialised through ``UserOut``.

    Raises:
        ApiError: ``UNAUTHENTICATED`` with status 401, raised by the dependency
            when the token is missing, invalid, or names no existing user
            (FR-7).
    """
    return user


@admin_router.post("/users/{user_id}/promote", response_model=UserOut)
async def promote_user(
    user_id: int,
    admin: Annotated[UserRecord, Depends(require_admin)],
    conn: DbConnection,
) -> UserRecord:
    """Give another account the ``admin`` role (SPEC-2 FR-9).

    Args:
        user_id: The target account's primary key, taken from the path.
        admin: The acting administrator, resolved by ``require_admin``. Its id
            is recorded in the audit log line and never written to the target
            row.
        conn: The request-scoped connection, shared with the dependency above.

    Returns:
        UserRecord: The target account, now carrying the role ``admin``.
        Promoting an account that is already an admin changes nothing and still
        answers 200 (SPEC-2 Section 3).

    Raises:
        ApiError: ``FORBIDDEN`` with status 403 for an authenticated non-admin,
            or ``UNAUTHENTICATED`` with status 401 for an anonymous caller
            (both from ``require_admin``, FR-8); ``USER_NOT_FOUND`` with status
            404 when no user has that id (FR-9).
    """
    promoted = await promote_to_admin(conn, user_id, admin.id)
    if promoted is None:
        raise ApiError("USER_NOT_FOUND", "No user with that id exists.", 404)
    return promoted
