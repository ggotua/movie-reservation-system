"""Unit test for the ``JWT_SECRET`` length rule (SPEC-2 Section 5, test 11).

The settings object is built from explicit values with ``_env_file=None`` so
the test never depends on ``backend/.env`` or the surrounding environment.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import Settings

# A placeholder URL: these tests only exercise the JWT_SECRET rule, so this
# value is never used to make a connection.
_DATABASE_URL = "postgresql+psycopg://user:placeholder@localhost:5432/database"


def _settings(jwt_secret: str) -> Settings:
    """Build a settings object from explicit values, ignoring any ``.env``.

    Args:
        jwt_secret: The ``JWT_SECRET`` value to validate.

    Returns:
        Settings: The validated settings, or a raised error if invalid.
    """
    return Settings(
        _env_file=None,
        DATABASE_URL=_DATABASE_URL,
        JWT_SECRET=jwt_secret,
    )


def test_settings_reject_short_jwt_secret() -> None:
    """A 31-character secret is rejected; a 32-character secret is accepted."""
    with pytest.raises(ValidationError):
        _settings("x" * 31)

    accepted = _settings("x" * 32)

    assert accepted.jwt_secret == "x" * 32
