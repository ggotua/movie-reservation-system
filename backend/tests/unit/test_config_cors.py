"""Unit test for the ``CORS_ORIGINS`` setting (SPEC-7 Section 5.1, test 1).

Settings are built from explicit values with ``_env_file=None`` so the test never
depends on ``backend/.env`` or the surrounding environment, and no database is
touched: only the parsing and validation of the setting are exercised.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import Settings

# Placeholder values: these tests only exercise CORS_ORIGINS, so the database URL
# is never used to connect and the secret is a throwaway.
_DATABASE_URL = "postgresql+psycopg://user:placeholder@localhost:5432/database"
_JWT_SECRET = "x" * 32


def _default_settings() -> Settings:
    """Build settings without ``CORS_ORIGINS`` so its default applies.

    Returns:
        Settings: The validated settings, ignoring any ``backend/.env``.
    """
    return Settings(
        _env_file=None,
        DATABASE_URL=_DATABASE_URL,
        JWT_SECRET=_JWT_SECRET,
    )


def _settings_with_cors(cors_origins: str) -> Settings:
    """Build settings with an explicit ``CORS_ORIGINS`` string.

    Args:
        cors_origins: The raw comma-separated value to validate.

    Returns:
        Settings: The validated settings, or a raised error if invalid.
    """
    return Settings(
        _env_file=None,
        DATABASE_URL=_DATABASE_URL,
        JWT_SECRET=_JWT_SECRET,
        CORS_ORIGINS=cors_origins,
    )


def test_settings_cors_origins_parsing_and_rejects_wildcard() -> None:
    """``CORS_ORIGINS`` becomes a list and refuses ``*`` or a non-origin (FR-9).

    The default is the Vite development server written both ways; a supplied
    comma-separated value replaces it, entries are stripped, and a wildcard, an
    empty entry or anything that is not a bare ``http(s)`` origin stops settings
    creation instead of silently allowing an unintended browser origin.
    """
    # The default applies only when the variable is omitted.
    defaults = _default_settings()
    assert defaults.cors_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # One comma-separated string in, one ordered list out; whitespace is
    # trimmed and https with a host and no port is a valid origin too.
    parsed = _settings_with_cors(
        " https://app.example.com ,http://localhost:3000,https://cinema.example:8443"
    )
    assert parsed.cors_origins == [
        "https://app.example.com",
        "http://localhost:3000",
        "https://cinema.example:8443",
    ]

    # A wildcard -- alone or among valid entries -- is refused, and so is any
    # entry that is not ``scheme://host[:port]``: a trailing slash, a path, a
    # query, credentials, a missing/unknown scheme, an empty entry, or an empty
    # setting.
    rejected = (
        "*",
        "http://localhost:5173,*",
        "https://app.example.com/*",
        "http://localhost:5173/",
        "http://localhost:5173/app",
        "http://localhost:5173?debug=1",
        "http://localhost:5173#top",
        "localhost:5173",
        "ftp://localhost:5173",
        "http://user:password@localhost:5173",
        "http://localhost:5173,",
        "http://localhost:5173,,http://127.0.0.1:5173",
        "",
    )
    for value in rejected:
        with pytest.raises(ValidationError):
            _settings_with_cors(value)
