"""Seed data for the Movie Reservation System.

The API cannot create the rows this module writes: the first admin account
(``docs/APP-OVERVIEW.md`` Section 2.1 — "Seed data creates the first admin
account") and the fixed seat layouts of the cinema screens
(``docs/APP-OVERVIEW.md`` Section 2.3 — "A screen has a fixed seat layout
(rows x seats, seeded per screen)"). This script bootstraps both using the
table definitions in :mod:`src.db.tables`.

Usage::

    cd backend
    python -m src.db.seed

The admin credential is read from the ``SEED_ADMIN_EMAIL`` and
``SEED_ADMIN_PASSWORD`` environment variables (or ``backend/.env`` when
running from ``backend/``) and is hashed with bcrypt before it is stored, so
no secret ever appears in source (IMPLEMENTER RULES, rule 7).

Idempotency: every write is an ``INSERT ... ON CONFLICT DO NOTHING`` keyed on
the target table's natural unique constraint — ``uq_screens_name`` for a screen
and ``seats_unique_position`` for a seat. The admin insert instead uses an
untargeted ``ON CONFLICT DO NOTHING`` because ``users`` has two uniqueness rules
over the email column (``uq_users_email`` and the case-insensitive
``idx_users_email_lower``) and a collision on either must be skipped. Re-running
the script therefore creates no duplicate rows and raises no error.
"""

from __future__ import annotations

import asyncio
import sys

import bcrypt
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.enums import ADMIN_USER_ROLE
from src.db.engine import async_session_factory, engine
from src.db.tables import screens, seats, users

# The screens to seed, as (name, rows, seats_per_row). Row labels are derived
# from ``rows`` ('A'.. ), seat numbers run 1..``seats_per_row``.
_SCREEN_SPECS: tuple[tuple[str, int, int], ...] = (
    ("Screen 1", 8, 10),
    ("Screen 2", 6, 12),
)

# bcrypt is the project's password-hashing algorithm (docs/steering/tech-stack.md)
# and is called directly: hashpw/checkpw are the whole API, so the passlib
# wrapper added no value and its backend raises against bcrypt 5.x.
#
# TEMPORARY DUPLICATION — flagged for the DRY check in
# docs/steering/principles.md: SPEC-2 (auth) does not exist yet, so this is the
# only password-hashing code in the codebase. When SPEC-2 lands, hashing moves
# to a shared module under ``src/core/`` (docs/steering/conventions.md groups
# hashing there) and is imported by both the auth module and this seed. The
# algorithm and work factor must stay identical, otherwise a hash created by
# this seed would stop verifying at login.


def _hash_password(password: str) -> str:
    """Hash a plain-text password with bcrypt, generating a fresh salt.

    Args:
        password: The plain-text password to hash. It is UTF-8 encoded first,
            because bcrypt operates on bytes.

    Returns:
        str: The bcrypt hash, ASCII-encoded (e.g. ``$2b$12$...``), ready for
        ``users.password_hash``.

    Raises:
        ValueError: If the UTF-8 encoded password is longer than 72 bytes.
            bcrypt 5.x rejects such input instead of truncating it
            (docs/steering/tech-stack.md), so SPEC-2's request validation must
            cap the password length at 72 bytes before calling this function.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


class _SeedSettings(BaseSettings):
    """The credentials the seed reads from the environment.

    Mirrors :class:`src.core.config.Settings`: it reads ``backend/.env`` when
    running from ``backend/`` and then real environment variables (which take
    precedence), and it fails loudly when a required value is missing, so the
    seed sees the same values the application would and never falls back to a
    default credential.

    TEMPORARY HOME — flagged for the DRY check in docs/steering/principles.md:
    these two fields will likely belong in ``src/core/config.py``'s ``Settings``
    (docs/steering/conventions.md calls that module the single source of truth
    for runtime configuration). They are kept local for now so the application
    never depends on seed-only values and this module stays self-contained.

    Attributes:
        seed_admin_email: Email address of the first admin account, from
            ``SEED_ADMIN_EMAIL``. No default: a missing or blank value raises.
        seed_admin_password: Plain-text password of the first admin account,
            from ``SEED_ADMIN_PASSWORD``. No default. It is hashed before it is
            stored and is never written to disk in this form.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    seed_admin_email: str = Field(..., alias="SEED_ADMIN_EMAIL", min_length=1)
    seed_admin_password: str = Field(..., alias="SEED_ADMIN_PASSWORD", min_length=1)

    @field_validator("seed_admin_email", "seed_admin_password")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Reject whitespace-only credentials.

        ``min_length=1`` alone would accept ``"   "``, which is effectively
        unset; this makes such a configuration fail loudly instead, matching
        :class:`src.core.config.Settings`.

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


def _admin_user_statement(email: str, password_hash: str) -> Insert:
    """Build the idempotent insert for the first admin account.

    Args:
        email: The admin's email address (``SEED_ADMIN_EMAIL``).
        password_hash: The bcrypt hash of the admin's password. The plain-text
            password is never passed here.

    Returns:
        Insert: ``INSERT INTO users (...) ON CONFLICT DO NOTHING`` that skips
        the row when it collides with *any* uniqueness rule of ``users``.
    """
    return (
        pg_insert(users)
        .values(email=email, password_hash=password_hash, role=ADMIN_USER_ROLE)
        # No index_elements: users carries two uniqueness rules over email —
        # the case-sensitive constraint uq_users_email and the case-insensitive
        # index idx_users_email_lower (UNIQUE on lower(email)). Naming one would
        # let a re-run that differs only in letter case conflict on the unnamed
        # rule and abort with an IntegrityError, so a conflict on either rule is
        # skipped. screens/seats have a single rule each, so they keep their
        # explicit target.
        .on_conflict_do_nothing()
    )


def _screen_statement(name: str, rows: int, seats_per_row: int) -> Insert:
    """Build the idempotent insert for one screen's row.

    Args:
        name: The screen's unique name, e.g. ``"Screen 1"``.
        rows: Number of seat rows in the layout. Must be positive (the
            ``screens_rows_positive`` CHECK enforces it in the database).
        seats_per_row: Number of seats in each row. Must be positive (the
            ``screens_seats_per_row_positive`` CHECK enforces it).

    Returns:
        Insert: ``INSERT INTO screens (...) ON CONFLICT (name) DO NOTHING``,
        keyed on the natural unique constraint ``uq_screens_name``.
    """
    return (
        pg_insert(screens)
        .values(name=name, rows=rows, seats_per_row=seats_per_row)
        .on_conflict_do_nothing(index_elements=[screens.c.name])
    )


def _seat_rows(
    screen_id: int, rows: int, seats_per_row: int
) -> list[dict[str, int | str]]:
    """Build the seat rows for one screen's layout.

    Row labels are generated as upper-case letters starting at ``'A'`` (so
    ``rows=8`` yields ``'A'``..``'H'``) and seat numbers run ``1``..
    ``seats_per_row``, giving ``rows * seats_per_row`` seats in top-left to
    bottom-right order.

    Args:
        screen_id: Primary key of the screen the seats belong to.
        rows: Number of seat rows.
        seats_per_row: Number of seats in each row.

    Returns:
        list[dict[str, int | str]]: One mapping per seat, ready for
        :func:`_seats_statement`. ``seat_type`` is intentionally omitted so the
        column's ``'standard'`` server default applies.
    """
    return [
        {
            "screen_id": screen_id,
            "row_label": chr(ord("A") + row_index),
            "seat_number": seat_number,
        }
        for row_index in range(rows)
        for seat_number in range(1, seats_per_row + 1)
    ]


def _seats_statement(seat_rows: list[dict[str, int | str]]) -> Insert:
    """Build the idempotent bulk insert for a screen's seats.

    Args:
        seat_rows: Non-empty output of :func:`_seat_rows` for one screen.

    Returns:
        Insert: ``INSERT INTO seats (...) VALUES (...) ON CONFLICT
        (screen_id, row_label, seat_number) DO NOTHING``, keyed on the natural
        unique constraint ``seats_unique_position``.
    """
    return (
        pg_insert(seats)
        .values(seat_rows)
        .on_conflict_do_nothing(
            index_elements=[
                seats.c.screen_id,
                seats.c.row_label,
                seats.c.seat_number,
            ]
        )
    )


async def run_seed() -> None:
    """Seed the first admin account, the two screens and their seats.

    Runs everything in a single transaction so a failure part-way through leaves
    the database untouched. Every insert is ``ON CONFLICT DO NOTHING`` keyed on
    a natural unique constraint, so the function is idempotent: calling it twice
    inserts nothing the second time and does not raise.

    The admin password is read from the environment, hashed here with bcrypt,
    and only the hash is written to ``users.password_hash``.

    Side effects: opens and commits one database transaction, and disposes the
    process-wide async engine (``src.db.engine.engine``) before returning so a
    short-lived CLI process exits without leaving pooled connections open.

    Raises:
        pydantic.ValidationError: If ``SEED_ADMIN_EMAIL`` or
            ``SEED_ADMIN_PASSWORD`` is unset or blank — the seed fails loudly
            rather than creating an account with a default credential.
    """
    settings = _SeedSettings()  # Fails loudly here if either variable is unset.
    password_hash = _hash_password(settings.seed_admin_password)

    try:
        async with async_session_factory() as session, session.begin():
            # 1. Exactly one admin user.
            await session.execute(
                _admin_user_statement(settings.seed_admin_email, password_hash)
            )

            # 2. Each screen, then its seats. The id is re-read on every run
            #    because ON CONFLICT DO NOTHING returns no row when the
            #    screen already exists.
            for name, rows, seats_per_row in _SCREEN_SPECS:
                await session.execute(_screen_statement(name, rows, seats_per_row))
                screen_id = (
                    await session.execute(
                        select(screens.c.id).where(screens.c.name == name)
                    )
                ).scalar_one()
                await session.execute(
                    _seats_statement(_seat_rows(screen_id, rows, seats_per_row))
                )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if sys.platform == "win32":
        # Python on Windows defaults to the ProactorEventLoop, but async psycopg
        # refuses to run on it ("Psycopg cannot use the 'ProactorEventLoop' to
        # run in async mode"); it needs a SelectorEventLoop instead.
        asyncio.run(run_seed(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(run_seed())
