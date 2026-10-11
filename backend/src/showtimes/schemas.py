"""Pydantic request and response models for the screen and showtime endpoints.

These are the wire formats fixed by SPEC-3 Sections 2.3 and 2.4. They only shape
and validate: every business rule (screen-name uniqueness, existence of the
referenced movie and screen, the overlap rule, the browse window) lives in
:mod:`src.showtimes.service`, so this module imports nothing from it and no
response model carries a field the SPEC does not list.

Input rules implemented here, all from SPEC-3 Section 2.4:

* every request model is ``extra="forbid"`` (FR-4);
* screen ``name`` is stripped before its length is checked, so a value that is
  only whitespace fails the 1-character minimum;
* ``rows`` and ``seats_per_row`` are bounded so the generated row labels stay
  within ``A``..``Z`` and a screen cannot be absurdly wide;
* ``starts_at`` must carry a timezone and is normalised to UTC (FR-15); a naive
  datetime is a validation error rather than being interpreted in the server's
  local timezone (FR-10);
* ``price_cents`` is a strict integer from :data:`PRICE_CENTS_MIN` to
  :data:`PRICE_CENTS_MAX`, so a float or a numeric string is rejected instead of
  coerced (FR-10; ``principles.md``: all money is integer cents, never float).

Import direction (SPEC-3 Section 11.1): ``showtimes`` does not import ``movies``,
so the movie and screen references embedded in :class:`ShowtimeOut` are declared
here rather than reused from another module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# SPEC-3 Section 2.4 bounds, declared once and referenced by both the fields
# below and the service, so the numbers exist in exactly one place.
SCREEN_NAME_MAX_LENGTH = 50
SCREEN_ROWS_MIN = 1
SCREEN_ROWS_MAX = 26
SEATS_PER_ROW_MIN = 1
SEATS_PER_ROW_MAX = 40
PRICE_CENTS_MIN = 1
PRICE_CENTS_MAX = 100_000_000


def _normalise_to_utc(value: datetime) -> datetime:
    """Return ``value`` as the equivalent timezone-aware UTC instant.

    The stored instant is always UTC (SPEC-3 FR-15), so a value submitted with
    another offset (e.g. ``+04:00``) is converted to the same point in time
    rather than rejected (SPEC-3 Section 3, "``starts_at`` given with a non-UTC
    offset ... is accepted and stored as the equivalent UTC instant").

    Args:
        value: The submitted start instant.

    Returns:
        datetime: The same instant expressed in UTC.

    Raises:
        ValueError: If ``value`` carries no timezone information. A naive value
            would otherwise be read in the server's local timezone, making the
            stored instant depend on the deployment (SPEC-3 FR-10).
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("starts_at must include a timezone offset")
    return value.astimezone(UTC)


def _strip_name(value: str) -> str:
    """Strip a screen name and enforce its 1-50 character range.

    The check runs on the stripped text, so ``"  Hall 1  "`` and ``"Hall 1"``
    are the same name and a value of only whitespace counts as empty
    (SPEC-3 Section 2.4).

    Args:
        value: The submitted screen name.

    Returns:
        str: The stripped name.

    Raises:
        ValueError: If the stripped name is empty or longer than
            :data:`SCREEN_NAME_MAX_LENGTH`. Pydantic turns the ``ValueError``
            into a validation error, which the API reports as 422
            ``VALIDATION_ERROR`` (SPEC-3 FR-4).
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be empty")
    if len(stripped) > SCREEN_NAME_MAX_LENGTH:
        raise ValueError(f"name must be at most {SCREEN_NAME_MAX_LENGTH} characters")
    return stripped


class ScreenCreate(BaseModel):
    """The body of ``POST /admin/screens`` (SPEC-3 Sections 2.3, 2.4).

    Extra fields are forbidden (FR-4). The two bounds are what make the
    generated seat labels correct: ``rows`` never exceeds 26, so the labels run
    ``A``..``Z``, and ``seats_per_row`` never exceeds 40, so the numbering runs
    ``1``..``40`` (SPEC-3 FR-9).

    Attributes:
        name: The screen name. Stripped, then 1 to
            :data:`SCREEN_NAME_MAX_LENGTH` characters inclusive.
        rows: Number of seat rows, :data:`SCREEN_ROWS_MIN` to
            :data:`SCREEN_ROWS_MAX` inclusive.
        seats_per_row: Seats in each row, :data:`SEATS_PER_ROW_MIN` to
            :data:`SEATS_PER_ROW_MAX` inclusive.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    rows: Annotated[int, Field(ge=SCREEN_ROWS_MIN, le=SCREEN_ROWS_MAX)]
    seats_per_row: Annotated[int, Field(ge=SEATS_PER_ROW_MIN, le=SEATS_PER_ROW_MAX)]

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        """Strip ``name`` and enforce its 1-50 character range.

        Args:
            value: The submitted screen name.

        Returns:
            str: The stripped name.

        Raises:
            ValueError: If the stripped name is empty or too long.
        """
        return _strip_name(value)


class ScreenOut(BaseModel):
    """A screen as the API returns it (SPEC-3 Section 2.3).

    ``seat_count`` is not a column: it is ``rows * seats_per_row``, derived once
    by the service when it builds the record, so the reported count can never
    disagree with the dimensions beside it.

    Attributes:
        id: The screen's primary key.
        name: The stored name.
        rows: Number of seat rows.
        seats_per_row: Seats in each row.
        seat_count: Total number of seats on the screen
            (``rows * seats_per_row``).
    """

    id: int
    name: str
    rows: int
    seats_per_row: int
    seat_count: int


class MovieRef(BaseModel):
    """The movie reference embedded in a :class:`ShowtimeOut` (Section 2.3).

    Attributes:
        id: The movie's primary key.
        title: The stored title.
        poster_url: The stored poster URL, or ``None`` when there is no poster.
    """

    id: int
    title: str
    poster_url: str | None


class ScreenRef(BaseModel):
    """The screen reference embedded in a :class:`ShowtimeOut` (Section 2.3).

    Smaller than :class:`ScreenOut` on purpose: a showtime response carries only
    the screen's identity, not its seat layout.

    Attributes:
        id: The screen's primary key.
        name: The stored name.
    """

    id: int
    name: str


class ShowtimeCreate(BaseModel):
    """The body of ``POST /admin/showtimes`` (SPEC-3 Sections 2.3, 2.4).

    Extra fields are forbidden (FR-4). Existence of ``movie_id`` and
    ``screen_id`` is a business rule, not a shape rule, so it is checked by the
    service with a 404 (FR-5, decision D6).

    Attributes:
        movie_id: The id of the movie being scheduled.
        screen_id: The id of the screen the showtime plays on.
        starts_at: The start instant. Must carry a timezone; normalised to UTC
            by the validator below (FR-10, FR-15).
        price_cents: Ticket price in integer cents,
            :data:`PRICE_CENTS_MIN` to :data:`PRICE_CENTS_MAX` inclusive. A
            float or a numeric string is rejected (FR-10, FR-15).
    """

    model_config = ConfigDict(extra="forbid")

    movie_id: int
    screen_id: int
    starts_at: datetime
    price_cents: Annotated[
        int,
        Field(strict=True, ge=PRICE_CENTS_MIN, le=PRICE_CENTS_MAX),
    ]

    @field_validator("starts_at")
    @classmethod
    def _clean_starts_at(cls, value: datetime) -> datetime:
        """Require a timezone and normalise ``starts_at`` to UTC.

        Args:
            value: The submitted start instant.

        Returns:
            datetime: The same instant in UTC.

        Raises:
            ValueError: If ``value`` is naive (SPEC-3 FR-10).
        """
        return _normalise_to_utc(value)


class ShowtimeOut(BaseModel):
    """A showtime as the API returns it (SPEC-3 Section 2.3).

    The movie and screen data come from one query with a JOIN, never one query
    per row (SPEC-3 Section 2.5, "Browse showtimes"). Seat availability is
    SPEC-4 and deliberately absent here.

    Attributes:
        id: The showtime's primary key.
        starts_at: The start instant, in UTC.
        price_cents: Ticket price in integer cents.
        movie: The scheduled movie's identity and poster.
        screen: The screen's identity.
    """

    id: int
    starts_at: datetime
    price_cents: int
    movie: MovieRef
    screen: ScreenRef
