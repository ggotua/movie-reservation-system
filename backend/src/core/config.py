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
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
