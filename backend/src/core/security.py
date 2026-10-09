"""Pure security primitives: email normalization, password hashing and JWTs.

This module is the single home for password hashing and token handling
(SPEC-2 FR-11), used by both the ``auth`` module and ``src/db/seed.py`` so the
bcrypt algorithm and work factor can never drift apart. It is deliberately
pure: no database, no HTTP, no settings import, and no logging of secrets,
tokens, passwords or hashes (SPEC-2 Section 11). ``core`` imports nothing else
from the project (SPEC-2 Section 11.1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import cache

import bcrypt
import jwt

# bcrypt operates on the UTF-8 bytes of the password and rejects input longer
# than 72 bytes (docs/steering/tech-stack.md, SPEC-2 Section 2.3).
_BCRYPT_MAX_PASSWORD_BYTES = 72

# JWT signing algorithm and the only algorithm accepted when decoding; anything
# else (including "none") must be rejected (SPEC-2 Section 2.4, FR-7).
_JWT_ALGORITHM = "HS256"

# A fixed, non-secret, throwaway input. Its only purpose is to give an
# unknown-email login the same bcrypt work as a known one (SPEC-2 Section 2.6,
# FR-5); it is not a credential and is never matched against a real account.
_DUMMY_PASSWORD = "not-a-real-password-used-only-to-equalize-login-timing"


def normalize_email(email: str) -> str:
    """Normalize an email for storage and lookup.

    Args:
        email: The raw email address.

    Returns:
        str: The address with surrounding whitespace stripped, then lowercased,
        so ``" A@X.com "`` and ``"a@x.com"`` resolve to the same account
        (SPEC-2 Section 2.3).
    """
    return email.strip().lower()


def hash_password(password: str) -> str:
    """Hash a plain-text password with bcrypt, generating a fresh salt.

    Args:
        password: The plain-text password; it is UTF-8 encoded before hashing
            because bcrypt operates on bytes.

    Returns:
        str: The bcrypt hash, ASCII-encoded (e.g. ``$2b$12$...``), ready for
        ``users.password_hash``.

    Raises:
        ValueError: If the UTF-8 encoded password is longer than 72 bytes.
            bcrypt 5.x rejects such input instead of truncating it, so callers
            must cap the password length first (SPEC-2 Section 2.3, FR-11).
    """
    encoded = password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError("password must be at most 72 bytes in UTF-8")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plain-text password against a stored bcrypt hash.

    Never raises: a caller that cannot tell a wrong password from a malformed
    hash is exactly what avoids leaking which part failed (SPEC-2 FR-5, FR-7).

    Args:
        password: The plain-text password to check.
        password_hash: The stored bcrypt hash (a ``$2b$...`` string).

    Returns:
        bool: ``True`` only when the password matches the hash. ``False`` is
        returned for a wrong password, for a password longer than 72 bytes in
        UTF-8 (bcrypt rejects such input), and for any malformed hash.
    """
    try:
        encoded = password.encode("utf-8")
        if len(encoded) > _BCRYPT_MAX_PASSWORD_BYTES:
            return False
        return bcrypt.checkpw(encoded, password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # bcrypt raises ValueError for a malformed hash/salt; UnicodeEncodeError
        # (raised by .encode("ascii") on a non-ASCII hash) is a ValueError too.
        return False


@cache
def dummy_password_hash() -> str:
    """Return a cached bcrypt hash of a fixed throwaway string.

    Login verifies this dummy hash when the email is unknown, so an unknown
    email and a wrong password perform the same bcrypt work and cannot be told
    apart by response time (SPEC-2 Section 2.6, FR-5).

    Returns:
        str: A valid bcrypt hash of a non-secret constant, computed once per
        process and reused thereafter.
    """
    return hash_password(_DUMMY_PASSWORD)


def create_access_token(
    user_id: int,
    secret: str,
    expires_in_minutes: int,
    now: datetime | None = None,
) -> str:
    """Create a signed HS256 access token for a user.

    Args:
        user_id: The user's primary key; stored as the ``sub`` claim (string).
        secret: The signing key (``JWT_SECRET`` from settings).
        expires_in_minutes: Token lifetime in minutes; ``exp`` is ``iat`` plus
            this value (SPEC-2 FR-4).
        now: Issue time; defaults to the current UTC time. Supplied explicitly
            by tests to mint an already-expired token.

    Returns:
        str: The encoded JWT with ``sub``, ``iat`` and ``exp`` claims. The role
        is intentionally not included (SPEC-2 Section 2.4).
    """
    issued_at = now if now is not None else datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=expires_in_minutes)
    payload: dict[str, object] = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(payload, secret, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> int | None:
    """Decode and validate an access token, returning its user id.

    Rejects every invalid token by returning ``None`` rather than raising, so
    callers translate it to a single 401 (SPEC-2 FR-7). Decoding pins
    ``algorithms=["HS256"]`` and requires both ``exp`` and ``sub``.

    Args:
        token: The encoded JWT from the ``Authorization: Bearer`` header.
        secret: The signing key the token must have been signed with.

    Returns:
        int | None: The ``sub`` claim as an integer, or ``None`` for any
        invalid token: malformed, signed with a different secret, expired,
        using an algorithm other than HS256 (including ``none``), missing
        ``sub`` or ``exp``, or carrying a non-integer ``sub``.
    """
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except (jwt.PyJWTError, ValueError):
        return None

    subject = payload.get("sub")
    if not isinstance(subject, str):
        return None
    try:
        return int(subject)
    except ValueError:
        return None
