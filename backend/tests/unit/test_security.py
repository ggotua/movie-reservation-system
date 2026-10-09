"""Unit tests for the pure security primitives in :mod:`src.core.security`.

These cover SPEC-2 Section 5 tests 1-4 and 6-10. They exercise password hashing
and JWT handling directly and touch neither a database nor HTTP.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt

from src.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    normalize_email,
    verify_password,
)

# Test-only signing keys. They are fixed, non-production values (never read
# from the environment) used purely to sign tokens inside these tests.
_SECRET = "unit-test-signing-key-not-a-production-secret"
_OTHER_SECRET = "a-different-unit-test-signing-key-value"


def _base64url(data: bytes) -> str:
    """Encode ``data`` as unpadded base64url text, as a JWT segment.

    Args:
        data: The raw bytes to encode.

    Returns:
        str: The unpadded base64url-encoded text.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_normalize_email_strips_and_lowercases() -> None:
    """Surrounding whitespace is stripped and the address is lowercased."""
    assert normalize_email("  User@Example.COM  ") == "user@example.com"


def test_hash_and_verify_password_roundtrip() -> None:
    """A password verifies against its own bcrypt hash and is not stored raw."""
    password = "correct horse battery staple"

    password_hash = hash_password(password)

    assert password_hash != password
    assert password_hash.startswith("$2")
    assert verify_password(password, password_hash) is True


def test_verify_password_rejects_wrong_password() -> None:
    """A different password does not verify against the hash."""
    password_hash = hash_password("the-right-password")

    assert verify_password("the-wrong-password", password_hash) is False


def test_verify_password_rejects_password_over_72_bytes_without_raising() -> None:
    """A password over 72 UTF-8 bytes returns False instead of raising."""
    password_hash = hash_password("a-short-enough-password")

    assert verify_password("a" * 73, password_hash) is False


def test_token_roundtrip_returns_subject() -> None:
    """A token created for a user decodes back to that user's id."""
    token = create_access_token(
        user_id=42,
        secret=_SECRET,
        expires_in_minutes=60,
    )

    assert decode_access_token(token, _SECRET) == 42


def test_decode_rejects_expired_token() -> None:
    """A token whose ``exp`` is in the past decodes to ``None``."""
    issued_at = datetime.now(UTC) - timedelta(hours=2)

    token = create_access_token(
        user_id=7,
        secret=_SECRET,
        expires_in_minutes=60,
        now=issued_at,
    )

    assert decode_access_token(token, _SECRET) is None


def test_decode_rejects_bad_signature() -> None:
    """A token signed with one key is rejected when verified with another."""
    token = create_access_token(
        user_id=7,
        secret=_SECRET,
        expires_in_minutes=60,
    )

    assert decode_access_token(token, _OTHER_SECRET) is None


def test_decode_rejects_alg_none_token() -> None:
    """A hand-built unsigned (``alg: none``) token is rejected."""
    header = _base64url(json.dumps({"alg": "none", "typ": "JWT"}).encode("utf-8"))
    payload = _base64url(
        json.dumps(
            {
                "sub": "7",
                "exp": int((datetime.now(UTC) + timedelta(minutes=60)).timestamp()),
            }
        ).encode("utf-8")
    )
    unsigned_token = f"{header}.{payload}."

    assert decode_access_token(unsigned_token, _SECRET) is None


def test_decode_rejects_token_missing_sub_or_exp() -> None:
    """A correctly signed token lacking ``sub`` or ``exp`` is rejected."""
    expires_at = datetime.now(UTC) + timedelta(minutes=60)
    missing_sub = jwt.encode({"exp": expires_at}, _SECRET, algorithm="HS256")
    missing_exp = jwt.encode({"sub": "7"}, _SECRET, algorithm="HS256")

    assert decode_access_token(missing_sub, _SECRET) is None
    assert decode_access_token(missing_exp, _SECRET) is None
