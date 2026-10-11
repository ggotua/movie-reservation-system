"""Unit test for the CINEMA_TIMEZONE and SHOWTIME_SLOT_MINUTES settings (test 4).

Settings are built from explicit values with ``_env_file=None`` so the test
never depends on ``backend/.env`` or the surrounding environment.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import Settings

# Placeholder values: these tests only exercise the new settings, so the database
# URL is never used to connect and the secret is a throwaway.
_DATABASE_URL = "postgresql+psycopg://user:placeholder@localhost:5432/database"
_JWT_SECRET = "x" * 32


def _settings(*, cinema_timezone: str, showtime_slot_minutes: int) -> Settings:
    """Build settings from explicit values, ignoring any ``backend/.env``.

    Args:
        cinema_timezone: The ``CINEMA_TIMEZONE`` value to validate.
        showtime_slot_minutes: The ``SHOWTIME_SLOT_MINUTES`` value to validate.

    Returns:
        Settings: The validated settings, or a raised error if invalid.
    """
    return Settings(
        _env_file=None,
        DATABASE_URL=_DATABASE_URL,
        JWT_SECRET=_JWT_SECRET,
        CINEMA_TIMEZONE=cinema_timezone,
        SHOWTIME_SLOT_MINUTES=showtime_slot_minutes,
    )


def test_settings_reject_invalid_timezone_and_slot() -> None:
    """Reject slot 29/601 and a bad IANA name; accept 30/600 and defaults."""
    # Defaults apply when neither setting is supplied.
    defaults = Settings(
        _env_file=None,
        DATABASE_URL=_DATABASE_URL,
        JWT_SECRET=_JWT_SECRET,
    )
    assert defaults.cinema_timezone == "Asia/Tbilisi"
    assert defaults.showtime_slot_minutes == 180

    # Slot boundaries: 30 and 600 are inclusive, 29 and 601 are rejected.
    assert (
        _settings(
            cinema_timezone="Asia/Tbilisi", showtime_slot_minutes=30
        ).showtime_slot_minutes
        == 30
    )
    assert (
        _settings(
            cinema_timezone="Asia/Tbilisi", showtime_slot_minutes=600
        ).showtime_slot_minutes
        == 600
    )
    for slot in (29, 601):
        with pytest.raises(ValidationError):
            _settings(cinema_timezone="Asia/Tbilisi", showtime_slot_minutes=slot)

    # A real IANA name is accepted; unknown names are rejected.
    accepted = _settings(cinema_timezone="Europe/Berlin", showtime_slot_minutes=180)
    assert accepted.cinema_timezone == "Europe/Berlin"
    for bad_timezone in ("Not/AZone", "Nowhere"):
        with pytest.raises(ValidationError):
            _settings(cinema_timezone=bad_timezone, showtime_slot_minutes=180)
