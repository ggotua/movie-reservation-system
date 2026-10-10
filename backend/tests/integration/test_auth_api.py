"""Integration tests for the SPEC-2 auth and admin API.

These cover SPEC-2 Section 5 tests 12 to 29. Every test drives the REAL
application through an ASGI client (``httpx.ASGITransport``), never a live
server: no port is opened and no other process is needed. The database is the
real PostgreSQL named by ``TEST_DATABASE_URL`` -- the same setting the SPEC-1
tests use -- so the tests prove the SQL, the constraints and the HTTP layer
together (``tests/conftest.py`` holds every fixture).

Isolation: the shared ``client`` fixture hands every request of one test ONE
connection inside one outer transaction, and that transaction is rolled back at
teardown, so no test ever commits. The single exception is test 18
(``test_concurrent_signup_same_email_exactly_one_succeeds``), which needs two
simultaneous requests on separate committed connections and deletes its own rows
afterwards.

Test names match SPEC-2 Section 5 exactly, and each test carries a comment
naming the FR (SPEC-2 Section 6) it verifies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from sqlalchemy import Table, delete, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.core.enums import ADMIN_USER_ROLE, DEFAULT_USER_ROLE
from src.core.security import create_access_token, verify_password
from src.db.seed import seed_database
from src.db.tables import screens, seats, users

# The four endpoints (SPEC-2 Section 2.2). ``_PROMOTE_URL`` takes the target's
# id because the route declares it in the path.
_SIGNUP_URL = "/auth/signup"
_LOGIN_URL = "/auth/login"
_ME_URL = "/auth/me"
_PROMOTE_URL = "/admin/users/{user_id}/promote"

# A throwaway password that satisfies the policy: more than 8 characters and far
# under 72 bytes. It is a local test value, never a credential and never read
# from the environment.
_TEST_PASSWORD = "correct horse battery staple"

# The password boundaries of SPEC-2 Section 6 (Section 5, test 16):
# 7 characters (one short of the minimum), 8 (the minimum), exactly 72 bytes
# (bcrypt's limit) and 73 (one over it).
_SHORT_PASSWORD = "a" * 7
_MINIMUM_PASSWORD = "a" * 8
_MAXIMUM_PASSWORD = "a" * 72
_OVER_LONG_PASSWORD = "a" * 73

# One Georgian letter (U+10D0) is three bytes in UTF-8, so 24 letters are
# exactly 72 bytes and accepted while 25 are 75 bytes and rejected. Written as
# an escape sequence so this file stays ASCII-only.
_GEORGIAN_LETTER = "\u10d0"
_GEORGIAN_PASSWORD = _GEORGIAN_LETTER * 24
_GEORGIAN_PASSWORD_TOO_LONG = _GEORGIAN_LETTER * 25

# A primary key no real row can hold: the target of the 404 case (FR-9).
_UNKNOWN_USER_ID = 9_223_372_036_854_775_807

# The admin address handed to ``seed_database`` in test 29 (FR-11). It is
# deliberately lowercase-normalized in one constant so the assertion on the
# stored value does not restate the normalization rule.
_SEED_ADMIN_EMAIL = "Spec2.Seed.Admin@Example.com"
_SEED_ADMIN_EMAIL_NORMALIZED = "spec2.seed.admin@example.com"

# The signatures of the helpers the conftest fixtures hand to a test, so a test
# body stays readable without importing the fixture functions themselves.
_CreateUser = Callable[[httpx.AsyncClient, str, str], Awaitable[httpx.Response]]
_Login = Callable[[httpx.AsyncClient, str, str], Awaitable[str]]
_MakeAdmin = Callable[[AsyncConnection, int], Awaitable[None]]
_AuthHeader = Callable[[str], dict[str, str]]


def _credentials(email: str, password: str = _TEST_PASSWORD) -> dict[str, str]:
    """Build a signup or login body.

    Args:
        email: The submitted address.
        password: The submitted password; defaults to the policy-valid test
            password.

    Returns:
        dict[str, str]: ``{"email": ..., "password": ...}`` as JSON.
    """
    return {"email": email, "password": password}


def _promote_url(user_id: int) -> str:
    """Build the promote path for one target account.

    Args:
        user_id: The target's primary key.

    Returns:
        str: The path with the id substituted.
    """
    return _PROMOTE_URL.format(user_id=user_id)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Return a response body as a JSON object.

    Args:
        response: The response to read.

    Returns:
        dict[str, Any]: The decoded body.
    """
    body: dict[str, Any] = response.json()
    return body


def _error_payload(response: httpx.Response) -> dict[str, str]:
    """Return the inner ``error`` object of a failure response (FR-12).

    Args:
        response: A response whose body uses the standard error shape.

    Returns:
        dict[str, str]: The ``{"code": ..., "message": ...}`` object.
    """
    error: dict[str, str] = _json_object(response)["error"]
    return error


async def _count_rows(conn: AsyncConnection, table: Table) -> int:
    """Count every row of one table.

    Used by test 29 to prove a second seeding run inserts nothing.

    Args:
        conn: The open connection the test shares with the application.
        table: The table to count.

    Returns:
        int: The row count.
    """
    result = await conn.execute(select(func.count()).select_from(table))
    return int(result.scalar_one())


async def _count_users_with_email(conn: AsyncConnection, email: str) -> int:
    """Count the accounts stored under one normalized address.

    Counted on ``LOWER(email)``, matching the lookups in
    :mod:`src.auth.service`, so a mis-cased row would still be seen.

    Args:
        conn: The open connection the test shares with the application.
        email: The already-lowercased address to look for.

    Returns:
        int: The number of matching rows.
    """
    result = await conn.execute(
        select(func.count())
        .select_from(users)
        .where(func.lower(users.c.email) == email)
    )
    return int(result.scalar_one())


# --- Signup (tests 12-18) ---------------------------------------------------


async def test_signup_creates_user_with_role_user_and_no_hash_in_response(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
) -> None:
    """Signup answers 201 with ``UserOut`` and never a password hash (FR-1).

    The response body carries exactly the four ``UserOut`` fields, so a hash
    cannot leak through an extra key, and the recorded address is the normalized
    one.
    """
    response = await create_user(client, "New.User@Example.com", _TEST_PASSWORD)

    assert response.status_code == 201, response.text
    body = _json_object(response)
    assert set(body) == {"id", "email", "role", "created_at"}
    assert body["role"] == DEFAULT_USER_ROLE
    assert body["email"] == "new.user@example.com"
    assert isinstance(body["id"], int)


async def test_signup_stores_bcrypt_hash_and_lowercased_email(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    rollback_connection: AsyncConnection,
) -> None:
    """Only a bcrypt hash of the password and the lowercased email are stored.

    The row is read back through the connection the application itself used, so
    what is asserted is the committed-in-this-transaction state the request
    produced (FR-1, FR-11).
    """
    response = await create_user(client, "Mixed.Case@Example.COM", _TEST_PASSWORD)
    assert response.status_code == 201, response.text

    user_id = int(_json_object(response)["id"])
    row = (
        await rollback_connection.execute(
            select(users.c.email, users.c.password_hash).where(users.c.id == user_id)
        )
    ).one()

    assert row.email == "mixed.case@example.com"
    assert row.password_hash != _TEST_PASSWORD
    assert row.password_hash.startswith("$2")
    assert verify_password(_TEST_PASSWORD, row.password_hash) is True


async def test_signup_duplicate_email_different_case_returns_409(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
) -> None:
    """An address differing only in letter case is the same account (FR-2).

    The second insert is refused by the database's case-insensitive unique index
    and translated to 409 ``EMAIL_ALREADY_REGISTERED``.
    """
    first = await create_user(client, "duplicate@example.com", _TEST_PASSWORD)
    assert first.status_code == 201, first.text

    second = await create_user(client, "Duplicate@Example.COM", _TEST_PASSWORD)

    assert second.status_code == 409, second.text
    assert _error_payload(second)["code"] == "EMAIL_ALREADY_REGISTERED"


async def test_signup_invalid_email_returns_422(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
) -> None:
    """A syntactically invalid address is refused with 422 (FR-3)."""
    response = await create_user(client, "not-an-email", _TEST_PASSWORD)

    assert response.status_code == 422, response.text
    assert _error_payload(response)["code"] == "VALIDATION_ERROR"


async def test_signup_password_boundaries_return_422_outside_policy(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    rollback_connection: AsyncConnection,
) -> None:
    """Accept 8 characters and 72 bytes; refuse 7 and 73 (FR-3).

    The Georgian cases pin the rule to UTF-8 BYTES rather than characters: 24
    letters are exactly 72 bytes and accepted, 25 are 75 bytes and refused. Every
    rejected request must also create nothing.
    """
    accepted = (
        ("policy-minimum@example.com", _MINIMUM_PASSWORD),
        ("policy-72-bytes@example.com", _MAXIMUM_PASSWORD),
        ("policy-georgian-72-bytes@example.com", _GEORGIAN_PASSWORD),
    )
    rejected = (
        ("policy-7-chars@example.com", _SHORT_PASSWORD),
        ("policy-73-bytes@example.com", _OVER_LONG_PASSWORD),
        ("policy-georgian-75-bytes@example.com", _GEORGIAN_PASSWORD_TOO_LONG),
    )

    for email, password in accepted:
        response = await create_user(client, email, password)
        assert response.status_code == 201, response.text

    for email, password in rejected:
        response = await create_user(client, email, password)
        assert response.status_code == 422, response.text
        assert _error_payload(response)["code"] == "VALIDATION_ERROR"

    refused_emails = [email for email, _ in rejected]
    stored = (
        await rollback_connection.execute(
            select(func.count())
            .select_from(users)
            .where(users.c.email.in_(refused_emails))
        )
    ).scalar_one()
    assert int(stored) == 0


async def test_signup_with_role_field_returns_422_and_creates_nothing(
    client: httpx.AsyncClient,
    rollback_connection: AsyncConnection,
) -> None:
    """An extra ``role`` field is refused and promotes nobody (FR-3).

    Extra fields are forbidden by the request model, so a client cannot ask for
    ``admin`` at signup; the rejected request must leave no row behind.
    """
    email = "self-promotion@example.com"
    response = await client.post(
        _SIGNUP_URL,
        json={
            "email": email,
            "password": _TEST_PASSWORD,
            "role": ADMIN_USER_ROLE,
        },
    )

    assert response.status_code == 422, response.text
    assert _error_payload(response)["code"] == "VALIDATION_ERROR"
    assert await _count_users_with_email(rollback_connection, email) == 0


async def test_concurrent_signup_same_email_exactly_one_succeeds(
    committing_client: httpx.AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """Two simultaneous signups of one address: exactly one 201 and one 409.

    This is the only test that COMMITS: a race needs two independent
    transactions, so each request gets its own connection and the second insert
    must see the first one's committed row. The row is deleted in ``finally`` so
    the shared test database is left as it was found (FR-2).
    """
    email = "concurrent.signup@example.com"

    try:
        responses = await asyncio.gather(
            committing_client.post(_SIGNUP_URL, json=_credentials(email)),
            committing_client.post(_SIGNUP_URL, json=_credentials(email)),
        )

        statuses = sorted(response.status_code for response in responses)
        assert statuses == [201, 409], [
            (response.status_code, response.text) for response in responses
        ]

        conflict = next(
            response for response in responses if response.status_code == 409
        )
        assert _error_payload(conflict)["code"] == "EMAIL_ALREADY_REGISTERED"

        async with async_engine.connect() as conn:
            assert await _count_users_with_email(conn, email) == 1
    finally:
        async with async_engine.begin() as conn:
            await conn.execute(delete(users).where(func.lower(users.c.email) == email))


# --- Login (tests 19-20) ----------------------------------------------------


async def test_login_success_returns_token_valid_for_configured_lifetime(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    jwt_secret: str,
    jwt_expiry_minutes: int,
) -> None:
    """Login returns a signed token whose claims last the configured lifetime.

    The token is decoded with the SAME secret and algorithm the service signs
    with, and ``expires_in`` must agree with the ``exp``-``iat`` gap (FR-4).
    """
    email = "login.success@example.com"
    assert (await create_user(client, email, _TEST_PASSWORD)).status_code == 201

    response = await client.post(_LOGIN_URL, json=_credentials(email, _TEST_PASSWORD))

    assert response.status_code == 200, response.text
    body = _json_object(response)
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == jwt_expiry_minutes * 60

    token: str = body["access_token"]
    claims = jwt.decode(token, jwt_secret, algorithms=["HS256"])
    assert claims["sub"] != email  # the subject is the id, not the address
    assert claims["exp"] - claims["iat"] == jwt_expiry_minutes * 60
    assert datetime.fromtimestamp(claims["exp"], UTC) > datetime.now(UTC)


async def test_login_wrong_password_and_unknown_email_return_identical_401(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
) -> None:
    """A bad password and an unknown address are indistinguishable (FR-5).

    Both answer the same status, the same code, the same message and the same
    challenge header, so the API does not reveal which addresses exist.
    """
    email = "login.identical@example.com"
    assert (await create_user(client, email, _TEST_PASSWORD)).status_code == 201

    wrong_password = await client.post(
        _LOGIN_URL, json=_credentials(email, "a different valid password")
    )
    unknown_email = await client.post(
        _LOGIN_URL, json=_credentials("login.nobody@example.com", _TEST_PASSWORD)
    )

    assert wrong_password.status_code == 401, wrong_password.text
    assert unknown_email.status_code == 401, unknown_email.text
    assert wrong_password.json() == unknown_email.json()
    assert _error_payload(wrong_password)["code"] == "INVALID_CREDENTIALS"
    assert wrong_password.headers["WWW-Authenticate"] == "Bearer"
    assert unknown_email.headers["WWW-Authenticate"] == "Bearer"


# --- Current user (tests 21-24) ---------------------------------------------


async def test_me_with_valid_token_returns_current_user(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """``GET /auth/me`` returns the caller's own account (FR-6).

    The payload must equal what signup returned for the same account.
    """
    email = "me.valid@example.com"
    created = await create_user(client, email, _TEST_PASSWORD)
    assert created.status_code == 201, created.text

    token = await login(client, email, _TEST_PASSWORD)
    response = await client.get(_ME_URL, headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert _json_object(response) == _json_object(created)


async def test_me_without_token_or_with_invalid_token_returns_401(
    client: httpx.AsyncClient,
    auth_header: _AuthHeader,
) -> None:
    """A missing token and an unparsable token both answer 401 (FR-6).

    Both are ``UNAUTHENTICATED`` with the ``Bearer`` challenge, and the two
    bodies are identical so no parsing detail leaks.
    """
    anonymous = await client.get(_ME_URL)
    garbage = await client.get(_ME_URL, headers=auth_header("not-a-jwt"))

    assert anonymous.status_code == 401, anonymous.text
    assert garbage.status_code == 401, garbage.text
    assert _error_payload(anonymous)["code"] == "UNAUTHENTICATED"
    assert anonymous.json() == garbage.json()
    assert anonymous.headers["WWW-Authenticate"] == "Bearer"


async def test_me_with_expired_token_returns_401(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    auth_header: _AuthHeader,
    jwt_secret: str,
    jwt_expiry_minutes: int,
) -> None:
    """A token expired by one second is refused; a fresh one is not (FR-6).

    The pair of tokens sits one second either side of the lifetime, which proves
    the refusal is expiry and not some unrelated claim mismatch.
    """
    email = "me.expiry@example.com"
    created = await create_user(client, email, _TEST_PASSWORD)
    assert created.status_code == 201, created.text
    user_id = int(_json_object(created)["id"])

    now = datetime.now(UTC)
    expired = create_access_token(
        user_id,
        jwt_secret,
        jwt_expiry_minutes,
        now=now - timedelta(minutes=jwt_expiry_minutes, seconds=1),
    )
    fresh = create_access_token(
        user_id,
        jwt_secret,
        jwt_expiry_minutes,
        now=now - timedelta(seconds=1),
    )

    accepted = await client.get(_ME_URL, headers=auth_header(fresh))
    refused = await client.get(_ME_URL, headers=auth_header(expired))

    assert accepted.status_code == 200, accepted.text
    assert refused.status_code == 401, refused.text
    assert _error_payload(refused)["code"] == "UNAUTHENTICATED"


async def test_me_with_token_of_deleted_user_returns_401(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
    rollback_connection: AsyncConnection,
) -> None:
    """Deleting an account makes its still-valid token useless (FR-6).

    The token is proven good first, then the row is deleted inside the test's own
    transaction and the very same token must stop working: the caller is reloaded
    from the database on every request.
    """
    email = "me.deleted@example.com"
    created = await create_user(client, email, _TEST_PASSWORD)
    assert created.status_code == 201, created.text
    user_id = int(_json_object(created)["id"])

    token = await login(client, email, _TEST_PASSWORD)
    before = await client.get(_ME_URL, headers=auth_header(token))
    assert before.status_code == 200, before.text

    await rollback_connection.execute(delete(users).where(users.c.id == user_id))

    after = await client.get(_ME_URL, headers=auth_header(token))
    assert after.status_code == 401, after.text
    assert _error_payload(after)["code"] == "UNAUTHENTICATED"


# --- Promotion (tests 25-27) ------------------------------------------------


async def test_promote_requires_admin_returns_403_for_user_and_401_for_anonymous(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
) -> None:
    """Only an administrator may promote: 401 anonymous, 403 for a user (FR-8).

    An authenticated non-admin is rejected at 403 (not 401) because the caller
    IS identified; the token simply does not carry the required role.
    """
    actor_email = "promote.actor@example.com"
    target_email = "promote.forbidden.target@example.com"
    assert (await create_user(client, actor_email, _TEST_PASSWORD)).status_code == 201
    target = await create_user(client, target_email, _TEST_PASSWORD)
    assert target.status_code == 201, target.text
    target_id = int(_json_object(target)["id"])

    token = await login(client, actor_email, _TEST_PASSWORD)
    anonymous = await client.post(_promote_url(target_id))
    forbidden = await client.post(_promote_url(target_id), headers=auth_header(token))

    assert anonymous.status_code == 401, anonymous.text
    assert _error_payload(anonymous)["code"] == "UNAUTHENTICATED"
    assert forbidden.status_code == 403, forbidden.text
    assert _error_payload(forbidden)["code"] == "FORBIDDEN"


async def test_promote_makes_target_admin_and_takes_effect_on_existing_token(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
    make_admin: _MakeAdmin,
    rollback_connection: AsyncConnection,
) -> None:
    """Promotion sets ``role`` to admin and applies without a re-login (FR-9).

    The newly promoted user's PRE-EXISTING token is reused after promotion: it
    must now be accepted where it was refused a moment earlier, because the role
    is read from the database on every request rather than trusted from the
    token (FR-6).
    """
    admin_email = "promote.admin@example.com"
    target_email = "promote.target@example.com"
    admin = await create_user(client, admin_email, _TEST_PASSWORD)
    assert admin.status_code == 201, admin.text
    admin_id = int(_json_object(admin)["id"])
    await make_admin(rollback_connection, admin_id)

    target = await create_user(client, target_email, _TEST_PASSWORD)
    assert target.status_code == 201, target.text
    target_id = int(_json_object(target)["id"])

    admin_token = await login(client, admin_email, _TEST_PASSWORD)
    target_token = await login(client, target_email, _TEST_PASSWORD)

    before = await client.post(
        _promote_url(admin_id), headers=auth_header(target_token)
    )
    assert before.status_code == 403, before.text

    promoted = await client.post(
        _promote_url(target_id), headers=auth_header(admin_token)
    )
    assert promoted.status_code == 200, promoted.text
    assert _json_object(promoted)["role"] == ADMIN_USER_ROLE

    after = await client.post(_promote_url(admin_id), headers=auth_header(target_token))
    assert after.status_code == 200, after.text


async def test_promote_is_idempotent_and_unknown_user_returns_404(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
    make_admin: _MakeAdmin,
    rollback_connection: AsyncConnection,
) -> None:
    """Promoting twice is harmless; promoting nobody is 404 (FR-9).

    The second call must return exactly what the first returned -- the row is
    updated to the same value, not duplicated or errored -- and an unknown id
    must be reported as ``USER_NOT_FOUND`` rather than silently succeeding.
    """
    admin_email = "promote.idempotent.admin@example.com"
    target_email = "promote.idempotent.target@example.com"
    admin = await create_user(client, admin_email, _TEST_PASSWORD)
    assert admin.status_code == 201, admin.text
    admin_id = int(_json_object(admin)["id"])
    await make_admin(rollback_connection, admin_id)

    target = await create_user(client, target_email, _TEST_PASSWORD)
    assert target.status_code == 201, target.text
    target_id = int(_json_object(target)["id"])

    token = await login(client, admin_email, _TEST_PASSWORD)
    headers = auth_header(token)

    first = await client.post(_promote_url(target_id), headers=headers)
    second = await client.post(_promote_url(target_id), headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert _json_object(first) == _json_object(second)
    assert _json_object(first)["role"] == ADMIN_USER_ROLE

    missing = await client.post(_promote_url(_UNKNOWN_USER_ID), headers=headers)
    assert missing.status_code == 404, missing.text
    assert _error_payload(missing)["code"] == "USER_NOT_FOUND"


# --- Error shape (test 28) --------------------------------------------------


async def test_error_responses_all_use_the_standard_shape(
    client: httpx.AsyncClient,
    create_user: _CreateUser,
    login: _Login,
    auth_header: _AuthHeader,
    make_admin: _MakeAdmin,
    rollback_connection: AsyncConnection,
) -> None:
    """Every failure of every source has one body shape (FR-12).

    Seven failures are provoked -- 401, 403, 404 from a handler, 404 and 405 from
    the router, 409 and 422 -- and each body must be exactly
    ``{"error": {"code": ..., "message": ...}}`` with the documented code. No body
    may echo the submitted password, and no code may be generic where a specific
    one is documented.

    The 500 handler is deliberately not provoked here: reaching it means the
    application broke, and its behaviour is pinned by the unit tests instead.
    """
    admin_email = "shape.admin@example.com"
    user_email = "shape.user@example.com"

    admin = await create_user(client, admin_email, _TEST_PASSWORD)
    assert admin.status_code == 201, admin.text
    admin_id = int(_json_object(admin)["id"])
    await make_admin(rollback_connection, admin_id)

    member = await create_user(client, user_email, _TEST_PASSWORD)
    assert member.status_code == 201, member.text
    member_id = int(_json_object(member)["id"])

    admin_token = await login(client, admin_email, _TEST_PASSWORD)
    member_token = await login(client, user_email, _TEST_PASSWORD)
    admin_headers = auth_header(admin_token)
    member_headers = auth_header(member_token)

    # (label, status, code, response, a secret string the body must not quote)
    duplicate = await create_user(client, admin_email, _TEST_PASSWORD)
    invalid = await client.post(
        _SIGNUP_URL, json={"email": "shape-invalid", "password": _TEST_PASSWORD}
    )

    cases: list[tuple[str, int, str, httpx.Response, str]] = [
        ("anonymous /auth/me", 401, "UNAUTHENTICATED", await client.get(_ME_URL), ""),
        (
            "non-admin promote",
            403,
            "FORBIDDEN",
            await client.post(_promote_url(member_id), headers=member_headers),
            "",
        ),
        (
            "promote unknown user",
            404,
            "USER_NOT_FOUND",
            await client.post(_promote_url(_UNKNOWN_USER_ID), headers=admin_headers),
            "",
        ),
        (
            "unknown route",
            404,
            "NOT_FOUND",
            await client.get("/auth/no-such-endpoint"),
            "",
        ),
        ("wrong method", 405, "METHOD_NOT_ALLOWED", await client.get(_SIGNUP_URL), ""),
        (
            "duplicate signup",
            409,
            "EMAIL_ALREADY_REGISTERED",
            duplicate,
            _TEST_PASSWORD,
        ),
        ("invalid payload", 422, "VALIDATION_ERROR", invalid, _TEST_PASSWORD),
    ]

    for label, status, code, response, secret in cases:
        assert response.status_code == status, f"{label}: {response.text}"

        body = _json_object(response)
        assert set(body) == {"error"}, label
        error = body["error"]
        assert set(error) == {"code", "message"}, label
        assert error["code"] == code, f"{label}: {error!r}"
        message = error["message"]
        assert isinstance(message, str) and message, label
        if secret:
            assert secret not in response.text, label


# --- Seeding (test 29) ------------------------------------------------------


async def test_seed_uses_shared_hashing_and_stays_idempotent(
    rollback_connection: AsyncConnection,
) -> None:
    """Seeding stores a bcrypt admin hash and a second run inserts nothing.

    The seed function is exercised directly on the test connection (it commits
    nothing, so the outer transaction rolls everything back) with an address of
    its own, and its password is verified with the same
    :func:`~src.core.security.verify_password` the API uses -- proving seed and
    login share one hashing implementation, which is why the seeded admin can log
    in. Counts of users, screens and seats must be unchanged by the second run
    (FR-10, FR-11).
    """
    users_before = await _count_rows(rollback_connection, users)
    screens_before = await _count_rows(rollback_connection, screens)
    seats_before = await _count_rows(rollback_connection, seats)

    await seed_database(rollback_connection, _SEED_ADMIN_EMAIL, _TEST_PASSWORD)
    users_after_first = await _count_rows(rollback_connection, users)
    screens_after_first = await _count_rows(rollback_connection, screens)
    seats_after_first = await _count_rows(rollback_connection, seats)

    # The admin is new data, so exactly one account joined; the two screens and
    # their seats are inserted only if the test database did not have them yet.
    assert users_after_first == users_before + 1
    assert screens_after_first >= screens_before
    assert seats_after_first >= seats_before

    await seed_database(rollback_connection, _SEED_ADMIN_EMAIL, _TEST_PASSWORD)

    assert await _count_rows(rollback_connection, users) == users_after_first
    assert await _count_rows(rollback_connection, screens) == screens_after_first
    assert await _count_rows(rollback_connection, seats) == seats_after_first

    row = (
        await rollback_connection.execute(
            select(users.c.email, users.c.role, users.c.password_hash).where(
                func.lower(users.c.email) == _SEED_ADMIN_EMAIL_NORMALIZED
            )
        )
    ).one()
    assert row.email == _SEED_ADMIN_EMAIL_NORMALIZED
    assert row.role == ADMIN_USER_ROLE
    assert row.password_hash != _TEST_PASSWORD
    assert row.password_hash.startswith("$2")
    assert verify_password(_TEST_PASSWORD, row.password_hash) is True
