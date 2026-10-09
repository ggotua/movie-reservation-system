"""Authentication business rules over an open database connection.

This module owns the signup, login, lookup and role-change rules from SPEC-2
Sections 2.1, 2.3 and 2.7 (FR-1, FR-2, FR-3, FR-5, FR-6, FR-9). It is
deliberately free of HTTP and FastAPI imports: every function takes an
already-open :class:`~sqlalchemy.ext.asyncio.AsyncConnection`, never opens one
itself and never commits, so the request-scoped transaction stays owned by
:func:`src.db.engine.get_connection` (SPEC-2 Section 2.7) and the same
functions can be driven straight from a test connection.

Password hashing and verification are not implemented here. They come from
:mod:`src.core.security`, the single implementation shared with the seed script
(SPEC-2 FR-11). Role values come from :mod:`src.core.enums`; this module
contains no bare ``"user"`` / ``"admin"`` literals (SPEC-2 Sections 2.3, 10).

Import direction: ``auth`` may import ``core`` and ``db``; nothing in ``core``
or ``db`` may import this module. :mod:`src.auth.schemas` imports
:func:`validate_password` from here, so this module must never import
``schemas`` (SPEC-2 Section 11.1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from src.core.enums import ADMIN_USER_ROLE, DEFAULT_USER_ROLE, UserRole
from src.core.errors import ApiError
from src.core.security import (
    dummy_password_hash,
    hash_password,
    normalize_email,
    verify_password,
)
from src.db.tables import users

logger = logging.getLogger(__name__)

# SPEC-2 Section 2.3: a password is at least 8 characters and at most 72 bytes
# in UTF-8 (bcrypt rejects anything longer), and an email address is at most
# 254 characters. Declared once here and imported by ``schemas`` so the policy
# numbers exist in exactly one place.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72
EMAIL_MAX_LENGTH = 254

# The two uniqueness rules that can reject an insert of an already-used email:
# the plain unique constraint on ``users.email`` and the case-insensitive
# functional unique index SPEC-1 defines on ``LOWER(email)``. Both are listed
# because a duplicate row violates both at once and PostgreSQL reports
# whichever one it happens to check first.
_EMAIL_CONFLICT_NAMES: tuple[str, ...] = ("idx_users_email_lower", "uq_users_email")


@dataclass(frozen=True, slots=True)
class UserRecord:
    """A user as the public API sees it.

    Carries no ``password_hash``: a credential never leaves the storage layer
    (SPEC-2 Section 2.2, "``UserOut`` never contains ``password_hash``").

    Attributes:
        id: The user's primary key.
        email: The stored address, already normalized (stripped, lowercased).
        role: The stored role. ``users.role`` is guarded by the
            ``users_role_check`` CHECK constraint, so this is always one of the
            values in :data:`src.core.enums.USER_ROLES`.
        created_at: Row creation time, in UTC.
    """

    id: int
    email: str
    role: UserRole
    created_at: datetime


def _user_record(row: Row[*tuple[Any, ...]]) -> UserRecord:
    """Build the public :class:`UserRecord` from a selected ``users`` row.

    Args:
        row: A row holding at least the ``id``, ``email``, ``role`` and
            ``created_at`` columns of ``users``. A ``password_hash`` column that
            is also present is deliberately dropped.

    Returns:
        UserRecord: The public view of the row, without the password hash.
    """
    return UserRecord(
        id=row.id,
        email=row.email,
        role=row.role,
        created_at=row.created_at,
    )


def _is_email_conflict(error: IntegrityError) -> bool:
    """Report whether an integrity error is an already-used email address.

    SPEC-2 Section 3 requires the insert to be attempted and the database's
    unique violation to be translated; every other integrity error (a CHECK
    violation, a bad foreign key) is a bug and must not be swallowed.

    Args:
        error: The :class:`~sqlalchemy.exc.IntegrityError` raised by the insert.

    Returns:
        bool: ``True`` when the violated rule is one of the unique rules over
        ``users.email``, ``False`` otherwise. The rule name is read from the
        driver's structured diagnostics when available (psycopg exposes it as
        ``error.orig.diag.constraint_name``) and from the driver's message
        otherwise.
    """
    constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if isinstance(constraint, str) and constraint in _EMAIL_CONFLICT_NAMES:
        return True
    message = str(error.orig)
    return any(name in message for name in _EMAIL_CONFLICT_NAMES)


def validate_password(password: str) -> None:
    """Enforce SPEC-2 Section 2.3's password policy.

    The byte length is what matters, not the character count: bcrypt operates
    on the UTF-8 encoding, so 24 Georgian letters (3 bytes each) are 72 bytes
    and allowed while 25 of them are 75 bytes and rejected.

    Args:
        password: The candidate plain-text password.

    Returns:
        None.

    Raises:
        ValueError: If ``password`` is shorter than
            :data:`PASSWORD_MIN_LENGTH` characters or its UTF-8 encoding is
            longer than :data:`PASSWORD_MAX_BYTES` bytes.
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"password must be at least {PASSWORD_MIN_LENGTH} characters")
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(
            f"password must be at most {PASSWORD_MAX_BYTES} bytes in UTF-8"
        )


async def signup(conn: AsyncConnection, email: str, password: str) -> UserRecord:
    """Create a new account with the ``user`` role (SPEC-2 FR-1, FR-2, FR-3).

    The email is normalized (stripped, lowercased) before it is stored and the
    password is checked against the policy and kept only as a bcrypt hash
    (SPEC-2 Sections 2.3, 2.7). Duplicate detection is the database's job:

    * the insert runs inside a SAVEPOINT (``begin_nested``) so a unique
      violation does not poison the surrounding request transaction, and
    * the address is *not* pre-checked with a ``SELECT``, because a
      check-then-insert would still lose a concurrent race and the pre-check
      would then be the only protection (SPEC-2 Section 3).

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        email: The requested address, in any case and with optional surrounding
            whitespace.
        password: The plain-text password.

    Returns:
        UserRecord: The created account, without its password hash.

    Raises:
        ValueError: If ``password`` violates the policy.
        ApiError: ``EMAIL_ALREADY_REGISTERED`` with status 409 when the
            normalized email already belongs to an account, including when a
            concurrent request inserted it first.
    """
    normalized_email = normalize_email(email)
    validate_password(password)
    password_hash = hash_password(password)

    statement = (
        insert(users)
        .values(
            email=normalized_email,
            password_hash=password_hash,
            role=DEFAULT_USER_ROLE,
        )
        .returning(users.c.id, users.c.email, users.c.role, users.c.created_at)
    )

    try:
        async with conn.begin_nested():
            result = await conn.execute(statement)
            row = result.one()
    except IntegrityError as error:
        if not _is_email_conflict(error):
            raise
        logger.info("signup_duplicate_email")
        raise ApiError(
            "EMAIL_ALREADY_REGISTERED",
            "An account with this email address already exists.",
            409,
        ) from error

    return _user_record(row)


async def authenticate(
    conn: AsyncConnection,
    email: str,
    password: str,
) -> UserRecord | None:
    """Verify a set of login credentials (SPEC-2 FR-5).

    The lookup compares ``LOWER(email)`` after normalizing the submitted
    address, so it matches the SPEC-1 functional unique index no matter how an
    older row was stored (SPEC-2 Section 2.3).

    Exactly one :func:`~src.core.security.verify_password` call happens on every
    path: when the address is unknown the submitted password is checked against
    a fixed dummy hash instead, so an unknown email and a wrong password do the
    same work and cannot be told apart by timing (SPEC-2 Section 2.6). Which
    half was wrong is never returned and never logged.

    Args:
        conn: An open connection. This function only reads.
        email: The submitted address, in any case.
        password: The submitted plain-text password. A password longer than 72
            bytes is simply a failed verification, never an error.

    Returns:
        UserRecord | None: The matching account, or ``None`` when the email is
        unknown or the password is wrong. The caller answers both the same way.
    """
    normalized_email = normalize_email(email)
    row = (
        await conn.execute(
            select(
                users.c.id,
                users.c.email,
                users.c.role,
                users.c.created_at,
                users.c.password_hash,
            ).where(func.lower(users.c.email) == normalized_email)
        )
    ).one_or_none()

    if row is None:
        verify_password(password, dummy_password_hash())
        logger.info("authentication_failed")
        return None

    if not verify_password(password, row.password_hash):
        logger.info("authentication_failed")
        return None

    return _user_record(row)


async def get_user_by_id(conn: AsyncConnection, user_id: int) -> UserRecord | None:
    """Load one account by primary key (SPEC-2 FR-6).

    Used on every authenticated request so the role comes from the database
    rather than from the token: a promotion (or a deleted account) then takes
    effect immediately and a token can never carry a stale role (SPEC-2
    Section 2.4).

    Args:
        conn: An open connection. This function only reads.
        user_id: The ``sub`` claim of a verified access token.

    Returns:
        UserRecord | None: The account, or ``None`` when no user has that id
        (the caller answers 401, never 500).
    """
    row = (
        await conn.execute(
            select(
                users.c.id,
                users.c.email,
                users.c.role,
                users.c.created_at,
            ).where(users.c.id == user_id)
        )
    ).one_or_none()
    return None if row is None else _user_record(row)


async def promote_to_admin(
    conn: AsyncConnection,
    user_id: int,
    actor_id: int,
) -> UserRecord | None:
    """Give an existing account the ``admin`` role (SPEC-2 FR-9).

    Idempotent: promoting an account that is already an admin writes the value
    it already holds and returns the row unchanged, so a repeat is still a 200
    and performs no second state change (SPEC-2 Section 3). Because the role is
    read from the database on every request instead of being carried in a token,
    the change applies to the target's existing token on their next call
    (SPEC-2 Section 2.4).

    Args:
        conn: An open connection. The caller owns the surrounding transaction
            and the commit; this function never commits.
        user_id: The target account's primary key.
        actor_id: The id of the signed-in admin performing the promotion. Used
            only for the audit log line and never written to the row.

    Returns:
        UserRecord | None: The target account with ``role`` set to ``admin``, or
        ``None`` when no user has that id (the caller answers 404).
    """
    row = (
        await conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(role=ADMIN_USER_ROLE)
            .returning(users.c.id, users.c.email, users.c.role, users.c.created_at)
        )
    ).one_or_none()

    if row is None:
        return None

    logger.info("user_promoted actor=%s target=%s", actor_id, user_id)
    return _user_record(row)
