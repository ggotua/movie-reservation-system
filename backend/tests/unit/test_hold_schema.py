"""Unit test for the HoldRequest shape (SPEC-4 Section 5, test 5).

No database and no HTTP: the Pydantic model is validated directly. The test
also pins the division of responsibility from FR-3 -- the upper seat bound is a
service rule, so the schema itself accepts any non-empty distinct strict-int
list.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.reservations.schemas import HoldRequest


def test_hold_schema_boundaries() -> None:
    """Test 5: empty/duplicate/coerced/extra input is rejected; 1 id is valid."""
    # The smallest valid request, and a larger one: no schema-level maximum.
    accepted = HoldRequest(showtime_id=1, seat_ids=[1])
    assert accepted.showtime_id == 1
    assert accepted.seat_ids == [1]

    nine_seats = list(range(1, 10))
    assert HoldRequest(showtime_id=1, seat_ids=nine_seats).seat_ids == nine_seats

    invalid_bodies: list[dict[str, object]] = [
        # Empty seat selection (at least one is required).
        {"showtime_id": 1, "seat_ids": []},
        # Duplicate seat ids.
        {"showtime_id": 1, "seat_ids": [1, 2, 1]},
        # Non-integer ids: numeric strings and floats are not coerced.
        {"showtime_id": 1, "seat_ids": ["1"]},
        {"showtime_id": 1, "seat_ids": [1.0]},
        {"showtime_id": 1, "seat_ids": [1.5]},
        # Extra fields are forbidden.
        {"showtime_id": 1, "seat_ids": [1], "user_id": 7},
        # showtime_id must be a real integer, not a string or a float.
        {"showtime_id": "1", "seat_ids": [1]},
        {"showtime_id": 1.0, "seat_ids": [1]},
    ]
    for body in invalid_bodies:
        with pytest.raises(ValidationError):
            HoldRequest.model_validate(body)
