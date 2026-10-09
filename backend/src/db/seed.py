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
running from ``backend/``) and is hashed with the shared
:func:`src.core.security.hash_password` before it is stored (SPEC-2 FR-11), so
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

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from src.core.enums import ADMIN_USER_ROLE
from src.core.security import hash_password, normalize_email
from src.db.engine import engine
from src.db.tables import screens, seats, users

# The screens to seed, as (name, rows, seats_per_row). Row labels are derived
# from ``rows`` ('A'.. ), seat numbers run 1..``seats_per_row``.
_SCREEN_SPECS: tuple[tuple[str, int, int], ...] = (
    ("Screen 1", 8, 10),
    ("Screen 2", 6, 12),
)

# SPEC-2 Section 2.3 password policy, applied to the seed credential so a
# misconfigured admin password fails loudly instead of being stored. The auth
# module will own the authoritative check (``validate_password``); this local
# copy exists only because that module does not exist yet and ``db`` must not
# depend on ``auth`` (SPEC-2 Section 11.1).
_MIN_PASSWORD_CHARS = 8
_MAX_PASSWORD_BYTES = 72


def _validate_seed_password(password: str) -> None:
    """Fail loudly when the seed admin password violates SPEC-2's policy.

    SPEC-2 Section 2.3 requires a password of at least 8 characters and at
    most 72 bytes in UTF-8. :func:`~src.core.security.hash_password` already
    rejects input over 72 bytes, but checking here gives the seed a clear,
    credential-specific message for both cases. Once the auth module exists,
    SPEC-2's ``validate_password`` becomes the single authoritative check.

    Args:
        password: The plain-text ``SEED_ADMIN_PASSWORD`` value.

    Returns:
        None.

    Raises:
        ValueError: If ``password`` is shorter than 8 characters or longer
            than 72 bytes in UTF-8. The message names the environment variable,
            never the supplied value.
    """
    if len(password) < _MIN_PASSWORD_CHARS:
        raise ValueError("SEED_ADMIN_PASSWORD must be at least 8 characters")
    if len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ValueError("SEED_ADMIN_PASSWORD must be at most 72 bytes in UTF-8")


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


async def seed_database(
    conn: AsyncConnection,
    admin_email: str,
    admin_password: str,
) -> None:
    """Seed the admin account, the two screens and their seats.

    Infrastructure-free: it takes an already-open async connection plus the
    credentials, reads no settings or environment, opens no engine and commits
    nothing — the caller owns the transaction. That is what lets a test run the
    exact seeding logic against ``TEST_DATABASE_URL`` without any risk of
    touching the development database (SPEC-2 Section 4).

    Every insert is ``ON CONFLICT DO NOTHING`` keyed on a natural unique
    constraint, so the function is idempotent: calling it twice inserts nothing
    the second time and raises nothing. The admin email is stored normalized
    (stripped, lowercased) and only the bcrypt hash of the password is written.

    Args:
        conn: An open :class:`~sqlalchemy.ext.asyncio.AsyncConnection`. The
            caller owns the surrounding transaction and the commit.
        admin_email: The first admin's email (``SEED_ADMIN_EMAIL``); it is
            passed through :func:`~src.core.security.normalize_email`.
        admin_password: The first admin's plain-text password; it is validated
            against SPEC-2's policy and hashed with
            :func:`~src.core.security.hash_password`.

    Returns:
        None.

    Raises:
        ValueError: If ``admin_password`` is shorter than 8 characters or
            longer than 72 bytes in UTF-8 (see :func:`_validate_seed_password`).
    """
    _validate_seed_password(admin_password)
    normalized_email = normalize_email(admin_email)
    password_hash = hash_password(admin_password)

    # 1. Exactly one admin user.
    await conn.execute(_admin_user_statement(normalized_email, password_hash))

    # 2. Each screen, then its seats. The id is re-read on every run because
    #    ON CONFLICT DO NOTHING returns no row when the screen already exists.
    for name, rows, seats_per_row in _SCREEN_SPECS:
        await conn.execute(_screen_statement(name, rows, seats_per_row))
        screen_id = (
            await conn.execute(select(screens.c.id).where(screens.c.name == name))
        ).scalar_one()
        await conn.execute(_seats_statement(_seat_rows(screen_id, rows, seats_per_row)))


async def run_seed() -> None:
    """CLI entry point: read credentials, seed inside one transaction, commit.

    Reads ``SEED_ADMIN_EMAIL`` / ``SEED_ADMIN_PASSWORD`` through
    :class:`_SeedSettings` (fails loudly when either is unset or blank), opens
    one connection bound to the module engine with a transaction, delegates all
    seeding work to :func:`seed_database`, and commits when that returns. If
    anything raises, the transaction is rolled back and the database is left
    untouched.

    Side effects: opens and commits one database transaction, and disposes the
    process-wide async engine (``src.db.engine.engine``) before returning so a
    short-lived CLI process exits without leaving pooled connections open.

    Returns:
        None.

    Raises:
        pydantic.ValidationError: If ``SEED_ADMIN_EMAIL`` or
            ``SEED_ADMIN_PASSWORD`` is unset or blank — the seed fails loudly
            rather than creating an account with a default credential.
        ValueError: If the password violates SPEC-2's length policy.
    """
    settings = _SeedSettings()  # Fails loudly here if either variable is unset.

    try:
        async with engine.begin() as conn:
            await seed_database(
                conn,
                settings.seed_admin_email,
                settings.seed_admin_password,
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
