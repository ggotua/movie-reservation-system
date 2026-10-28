"""Unit test for the HOLD_MINUTES and MAX_SEATS_PER_HOLD settings (test 4).

Settings are built from explicit values with ``_env_file=None`` so the test
never depends on ``backend/.env`` or the surrounding environment.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import Settings

# Placeholder values: these tests only exercise the new settings, so the
# database URL is never used to connect and the secret is a throwaway.
_DATABASE_URL = "postgresql+psycopg://user:placeholder@localhost:5432/database"
_JWT_SECRET = "x" * 32


def _settings(*, hold_minutes: int, max_seats_per_hold: int) -> Settings:
    """Build settings from explicit values, ignoring any ``backend/.env``.

    Args:
        hold_minutes: The ``HOLD_MINUTES`` value to validate.
        max_seats_per_hold: The ``MAX_SEATS_PER_HOLD`` value to validate.

    Returns:
        Settings: The validated settings.

    Raises:
        pydantic.ValidationError: If either value is outside its allowed range.
    """
    return Settings(
        _env_file=None,
        DATABASE_URL=_DATABASE_URL,
        JWT_SECRET=_JWT_SECRET,
        HOLD_MINUTES=hold_minutes,
        MAX_SEATS_PER_HOLD=max_seats_per_hold,
    )


def test_settings_reject_invalid_hold_minutes_and_max_seats() -> None:
    """Test 4: reject hold 0/61 and max seats 0/21; accept 1/60 and 1/20."""
    # Defaults apply when neither new setting is supplied (SPEC-4 D1, D3).
    defaults = Settings(
        _env_file=None,
        DATABASE_URL=_DATABASE_URL,
        JWT_SECRET=_JWT_SECRET,
    )
    assert defaults.hold_minutes == 10
    assert defaults.max_seats_per_hold == 8

    # HOLD_MINUTES: 1 and 60 are inclusive; 0 and 61 are rejected.
    assert _settings(hold_minutes=1, max_seats_per_hold=8).hold_minutes == 1
    assert _settings(hold_minutes=60, max_seats_per_hold=8).hold_minutes == 60
    for hold_minutes in (0, 61):
        with pytest.raises(ValidationError):
            _settings(hold_minutes=hold_minutes, max_seats_per_hold=8)

    # MAX_SEATS_PER_HOLD: 1 and 20 are inclusive; 0 and 21 are rejected.
    assert _settings(hold_minutes=10, max_seats_per_hold=1).max_seats_per_hold == 1
    assert _settings(hold_minutes=10, max_seats_per_hold=20).max_seats_per_hold == 20
    for max_seats_per_hold in (0, 21):
        with pytest.raises(ValidationError):
            _settings(hold_minutes=10, max_seats_per_hold=max_seats_per_hold)
