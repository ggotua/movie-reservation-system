"""Application configuration read from environment variables.

This module is the single source of truth for runtime configuration so the
rest of the codebase never reads ``os.environ`` directly. Values are parsed
and validated by :class:`Settings`; access them through :func:`get_settings`.

Fail-loud behaviour: ``DATABASE_URL`` and ``JWT_SECRET`` are declared with
no default, so importing :func:`get_settings` (and therefore any module that
depends on it) raises immediately when either is unset or blank. In addition,
``JWT_SECRET`` must be at least 32 characters long: a shorter non-blank value
is rejected for the same reason (a weak signing key must not start silently).
A misconfigured deployment must not start silently.

``CORS_ORIGINS`` is the exception that proves the rule in the other direction:
it may be omitted, but when it is supplied every entry must be a bare origin
(scheme + host [+ port]). A wildcard ``*`` or any entry carrying a path, query,
fragment, credentials or a trailing slash stops startup, so a browser origin
that was never intended can never be allowed by accident (SPEC-7 D2, FR-9).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# The Vite development server, written the two ways a browser may spell it
# (SPEC-7 D2). This is a default only: the environment variable replaces the
# whole list, it is never extended.
_DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

# An origin is served over HTTP(S); anything else (e.g. ``ftp:`` or a bare
# ``localhost:5173`` that Python reads as scheme ``localhost``) is rejected.
_ALLOWED_ORIGIN_SCHEMES = frozenset({"http", "https"})


def _cors_origin_entries(value: object) -> list[str]:
    """Split the raw ``CORS_ORIGINS`` value into stripped, non-empty entries.

    The environment can only carry a string, so the field is annotated with
    :class:`~pydantic_settings.NoDecode` and split here instead of being parsed
    as JSON. A list or tuple is accepted as well, because that is how a test or
    another caller may supply the value directly.

    Args:
        value: The raw setting value: a comma-separated string, or a sequence
            of strings.

    Returns:
        list[str]: The entries with surrounding whitespace removed.

    Raises:
        ValueError: If ``value`` is neither a string nor a sequence of strings,
            or if splitting leaves no entry or an empty one (an empty setting
            must not silently mean "allow nothing" or "allow everything").
    """
    if isinstance(value, str):
        raw_entries: list[str] = value.split(",")
    elif isinstance(value, list | tuple):
        if not all(isinstance(entry, str) for entry in value):
            raise ValueError("CORS_ORIGINS entries must all be strings")
        raw_entries = [entry for entry in value if isinstance(entry, str)]
    else:
        raise ValueError("CORS_ORIGINS must be a comma-separated list of origins")

    entries = [entry.strip() for entry in raw_entries]
    if not entries or any(not entry for entry in entries):
        raise ValueError(
            "CORS_ORIGINS must list at least one origin and contain no empty entry"
        )
    return entries


def _validate_cors_origin(entry: str) -> str:
    """Reject a ``CORS_ORIGINS`` entry that is not a bare origin (SPEC-7 FR-9).

    ``allow_credentials`` is off and the origin list is compared literally by
    Starlette, so an entry with a path, a query or a trailing slash would never
    match a browser's ``Origin`` header. Such a value is a configuration bug,
    and it fails here rather than silently blocking every request.

    Args:
        entry: One stripped entry of the setting.

    Returns:
        str: The unchanged entry when it is a valid origin.

    Raises:
        ValueError: If the entry is the wildcard ``*``, or it is not
            ``scheme://host[:port]`` with an ``http``/``https`` scheme and no
            path, query, fragment, credentials or trailing slash.
    """
    if entry == "*":
        raise ValueError(
            "CORS_ORIGINS must not contain '*': list the allowed origins explicitly"
        )

    try:
        parts = urlsplit(entry)
        # Reading ``port`` validates its range, which ``urlsplit`` defers.
        _ = parts.port
    except ValueError as exc:
        raise ValueError(f"{entry!r} is not a valid origin") from exc

    if parts.scheme not in _ALLOWED_ORIGIN_SCHEMES:
        raise ValueError(
            f"{entry!r} is not a valid origin: the scheme must be http or https"
        )
    if parts.path or parts.query or parts.fragment:
        raise ValueError(
            f"{entry!r} is not a valid origin: no path, query, fragment or "
            "trailing slash is allowed"
        )
    if "@" in parts.netloc or not parts.hostname:
        raise ValueError(
            f"{entry!r} is not a valid origin: it must be scheme://host[:port]"
        )
    return entry


class Settings(BaseSettings):
    """Validated runtime settings loaded from environment variables.

    Reads ``backend/.env`` when present (commands run from ``backend/``), then
    overrides with real environment variables.

    Attributes:
        database_url: Async SQLAlchemy database URL using the psycopg driver
            (e.g. ``postgresql+psycopg://user:pass@host:5432/dbname``). No
            default: it carries credentials and must be provided explicitly.
        jwt_secret: Secret used to sign and verify JWTs. No default, so a
            missing or blank value fails loudly at startup; it must also be at
            least 32 characters long (SPEC-2 FR-10).
        jwt_expiry_minutes: JWT lifetime in minutes. Must be greater than 0.
        seat_hold_expiry_minutes: How long a seat hold stays active before
            it expires, in minutes. Must be greater than 0.
        cinema_timezone: IANA timezone name that defines a "calendar day" for
            browsing showtimes by date (SPEC-3 FR-13). Must be a known IANA
            name; an unknown one fails loudly at startup.
        showtime_slot_minutes: Minimum separation, in minutes, between two
            showtimes on the same screen (SPEC-3 FR-11). Must be between 30 and
            600 inclusive.
        hold_minutes: How long a reservation hold stays active before it
            expires, in minutes (SPEC-4 D1, FR-5). Must be between 1 and 60
            inclusive.
        max_seats_per_hold: Maximum number of seats one hold may contain
            (SPEC-4 D3). Must be between 1 and 20 inclusive.
        cors_origins: Browser origins allowed to call the API, decoded from the
            comma-separated ``CORS_ORIGINS`` variable (SPEC-7 D2, FR-9). Each
            entry must be ``scheme://host[:port]`` with an ``http`` or ``https``
            scheme and no path, query, fragment, credentials or trailing slash;
            ``*`` is refused.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    database_url: str = Field(..., alias="DATABASE_URL", min_length=1)
    jwt_secret: str = Field(..., alias="JWT_SECRET", min_length=1)
    jwt_expiry_minutes: int = Field(60, alias="JWT_EXPIRY_MINUTES", gt=0)
    seat_hold_expiry_minutes: int = Field(15, alias="SEAT_HOLD_EXPIRY_MINUTES", gt=0)
    cinema_timezone: str = Field("Asia/Tbilisi", alias="CINEMA_TIMEZONE")
    showtime_slot_minutes: int = Field(
        180, alias="SHOWTIME_SLOT_MINUTES", ge=30, le=600
    )
    # SPEC-4 FR-13: a window or seat cap outside these bounds stops startup
    # rather than silently changing the reservation rules.
    hold_minutes: int = Field(10, alias="HOLD_MINUTES", ge=1, le=60)
    max_seats_per_hold: int = Field(8, alias="MAX_SEATS_PER_HOLD", ge=1, le=20)
    # SPEC-7 D2 / FR-9: ``NoDecode`` keeps the environment's comma-separated
    # string intact so the validator below can split it; without it
    # pydantic-settings would try to read a JSON array from the value.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(_DEFAULT_CORS_ORIGINS),
        alias="CORS_ORIGINS",
    )

    @field_validator("jwt_secret", "database_url")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Reject whitespace-only values for required string settings.

        ``min_length=1`` alone would accept a value of ``"   "``, which is
        effectively unset — this validator makes such a configuration fail
        loudly instead.

        Args:
            value: The raw value supplied via the environment.

        Returns:
            str: The unchanged value when it contains non-whitespace.

        Raises:
            ValueError: If ``value`` is empty or only whitespace.
        """
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _require_minimum_secret_length(cls, value: str) -> str:
        """Reject a ``JWT_SECRET`` shorter than 32 characters (SPEC-2 FR-10).

        A short signing key is brute-forceable, so a non-blank value below the
        minimum stops startup rather than being silently accepted. Blankness is
        left to :meth:`_reject_blank` so its message is unchanged.

        Args:
            value: The raw ``JWT_SECRET`` value supplied via the environment.

        Returns:
            str: The unchanged value when it is blank or at least 32 characters.

        Raises:
            ValueError: If ``value`` is non-blank and shorter than 32 characters.
        """
        if value.strip() and len(value) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return value

    @field_validator("cinema_timezone")
    @classmethod
    def _require_known_iana_timezone(cls, value: str) -> str:
        """Reject a ``CINEMA_TIMEZONE`` that is not a known IANA name (FR-13).

        The name selects the calendar day used to browse showtimes, so an
        unknown name would make every date query wrong. Validating it here makes
        a misconfigured deployment fail loudly at startup instead.

        Args:
            value: The raw ``CINEMA_TIMEZONE`` value supplied via the
                environment.

        Returns:
            str: The unchanged value when ``zoneinfo`` can load it.

        Raises:
            ValueError: If the value is not a valid IANA timezone name.
                ``zoneinfo`` raises ``ZoneInfoNotFoundError`` for an unknown
                key and ``ValueError`` for a malformed one; both are translated
                so the message names the offending setting.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{value!r} is not a valid IANA timezone name") from exc
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: object) -> list[str]:
        """Split and validate ``CORS_ORIGINS`` into a list of origins (FR-9).

        Runs before pydantic validates the field, because the environment
        supplies one comma-separated string rather than a list.

        Args:
            value: The raw setting value supplied via the environment (a
                string), a sequence of strings supplied by a caller, or the
                default list.

        Returns:
            list[str]: The allowed origins in configured order.

        Raises:
            ValueError: If the value is empty, contains an empty entry, or any
                entry is ``*`` or is not a bare ``http(s)`` origin.
        """
        entries = _cors_origin_entries(value)
        return [_validate_cors_origin(entry) for entry in entries]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance.

    The result is cached so validation runs once per process and callers
    always receive the same immutable settings object.

    Returns:
        Settings: The validated settings loaded from the environment.

    Raises:
        pydantic.ValidationError: If a required variable (``DATABASE_URL``
            or ``JWT_SECRET``) is unset or blank, or any value fails its
            constraint (e.g. a non-positive expiry).
    """
    return Settings()  # Values come from the environment; plugin supplies types.
