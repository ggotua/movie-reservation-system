"""Unit tests for the pure reservation arithmetic (SPEC-4 Section 5, tests 1-3).

No database and no HTTP are involved: every function under test is called
directly with plain values. All instants are fixed and timezone-aware so the
results never depend on the machine clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.reservations.rules import (
    hold_cutoff,
    hold_expires_at,
    is_hold_expired,
    seat_state,
)

# The default window from SPEC-4 D1 (HOLD_MINUTES = 10).
_HOLD_MINUTES = 10

# An arbitrary but fixed, timezone-aware instant.
_BASE = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def test_hold_expiry_boundary() -> None:
    """Test 1: one second early is active; exactly at expiry is expired."""
    # One second before created_at + HOLD: still active.
    assert (
        is_hold_expired(
            _BASE,
            _BASE + timedelta(minutes=_HOLD_MINUTES) - timedelta(seconds=1),
            _HOLD_MINUTES,
        )
        is False
    )

    # Exactly created_at + HOLD: expired (the boundary belongs to "expired").
    assert (
        is_hold_expired(_BASE, _BASE + timedelta(minutes=_HOLD_MINUTES), _HOLD_MINUTES)
        is True
    )

    # One second after: expired.
    assert (
        is_hold_expired(
            _BASE,
            _BASE + timedelta(minutes=_HOLD_MINUTES) + timedelta(seconds=1),
            _HOLD_MINUTES,
        )
        is True
    )

    # The SQL cutoff expresses the same boundary: a held row is expired iff
    # created_at <= hold_cutoff(now), and only then.
    now = _BASE + timedelta(minutes=_HOLD_MINUTES)
    cutoff = hold_cutoff(now, _HOLD_MINUTES)
    assert cutoff == _BASE
    assert is_hold_expired(_BASE, now, _HOLD_MINUTES) is True
    assert is_hold_expired(_BASE + timedelta(seconds=1), now, _HOLD_MINUTES) is False

    # Naive datetimes are rejected rather than compared in local time.
    naive = datetime(2026, 10, 18, 12, 0)  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError):
        is_hold_expired(naive, now, _HOLD_MINUTES)
    with pytest.raises(ValueError):
        is_hold_expired(_BASE, naive, _HOLD_MINUTES)
    with pytest.raises(ValueError):
        hold_cutoff(naive, _HOLD_MINUTES)


def test_hold_expires_at_adds_configured_minutes() -> None:
    """Test 2: expiry is created_at plus the configured number of minutes."""
    assert hold_expires_at(_BASE, 1) == _BASE + timedelta(minutes=1)
    assert hold_expires_at(_BASE, _HOLD_MINUTES) == _BASE + timedelta(
        minutes=_HOLD_MINUTES
    )
    assert hold_expires_at(_BASE, 60) == _BASE + timedelta(minutes=60)

    # The supplied value is used; nothing is hard-coded to the default.
    assert hold_expires_at(_BASE, 7) == _BASE + timedelta(minutes=7)

    # The result keeps the aware instant's timezone.
    expires_at = hold_expires_at(_BASE, _HOLD_MINUTES)
    assert expires_at.tzinfo is not None
    assert expires_at.utcoffset() == timedelta(0)

    # A naive created_at cannot produce a well-defined expiry.
    naive = datetime(2026, 10, 18, 12, 0)  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError):
        hold_expires_at(naive, _HOLD_MINUTES)


def test_seat_state_derivation() -> None:
    """Test 3: no row/fresh/stale/confirmed/cancelled/expired map correctly."""
    created_at = _BASE
    five_minutes_later = _BASE + timedelta(minutes=5)
    ten_minutes_later = _BASE + timedelta(minutes=_HOLD_MINUTES)

    # No active row at all.
    assert seat_state(None, None, five_minutes_later, _HOLD_MINUTES) == "available"

    # A hold inside the window blocks the seat.
    assert seat_state("held", created_at, five_minutes_later, _HOLD_MINUTES) == "held"

    # A hold at exactly the window boundary is expired, so the seat is free.
    assert (
        seat_state("held", created_at, ten_minutes_later, _HOLD_MINUTES) == "available"
    )

    # A confirmed seat is taken regardless of age.
    assert (
        seat_state("confirmed", created_at, five_minutes_later, _HOLD_MINUTES)
        == "taken"
    )

    # Terminal statuses never block a seat.
    assert (
        seat_state("cancelled", created_at, five_minutes_later, _HOLD_MINUTES)
        == "available"
    )
    assert (
        seat_state("expired", created_at, five_minutes_later, _HOLD_MINUTES)
        == "available"
    )

    # A held status without a creation instant cannot be evaluated.
    with pytest.raises(ValueError):
        seat_state("held", None, five_minutes_later, _HOLD_MINUTES)

    # An unknown status is rejected instead of being reported as available.
    with pytest.raises(ValueError):
        seat_state("pending", created_at, five_minutes_later, _HOLD_MINUTES)

    # Naive instants are rejected.
    naive = datetime(2026, 10, 18, 12, 0)  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError):
        seat_state(None, None, naive, _HOLD_MINUTES)
    with pytest.raises(ValueError):
        seat_state("held", naive, five_minutes_later, _HOLD_MINUTES)
