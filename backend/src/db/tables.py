"""SQLAlchemy Core table definitions for the Movie Reservation System.

This module is the code implementation of the schema in
``docs/specs/SPEC-1-database-schema.md`` (Section 2) and should be read
alongside it. It defines one :class:`~sqlalchemy.MetaData` instance and one
:class:`~sqlalchemy.Table` object per table: ``users``, ``genres``, ``movies``,
``movie_genres``, ``screens``, ``seats``, ``showtimes``, ``reservations`` and
``seat_reservations``.

Design notes:

* Every constraint from SPEC-1 Section 2 is expressed here with SQLAlchemy
  Core constructs: ``CheckConstraint``, ``UniqueConstraint``, ``Index`` (with
  ``postgresql_where`` for the partial unique index on ``seat_reservations``)
  and foreign keys carrying the specified ``ON DELETE`` behavior.
* No ORM model classes are used — ``Table`` objects only — matching the
  deliberate exclusion of the SQLAlchemy ORM layer in
  ``docs/steering/tech-stack.md``. ``Table`` objects are declarative data, not
  classes with behavior, so they are consistent with the functional-style
  convention in ``docs/steering/conventions.md``.
* The shared role/status/seat-type vocabulary is imported from
  :mod:`src.core.enums` rather than restated as string literals (SPEC-1
  Section 9, DRY check).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)

from src.core.enums import (
    ACTIVE_RESERVATION_STATUSES,
    DEFAULT_RESERVATION_STATUS,
    DEFAULT_SEAT_TYPE,
    DEFAULT_USER_ROLE,
    RESERVATION_STATUSES,
    SEAT_TYPES,
    USER_ROLES,
)

# A deterministic naming convention for the constraints/indexes that SPEC-1
# Section 2 leaves unnamed. The SPEC-1 CHECK constraints and indexes carry
# explicit names that must match the spec exactly, so there is deliberately no
# "ck" entry here (its %(constraint_name)s token would prefix, and thus
# rename, those explicit names).
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

# --- users (SPEC-1 2.1) ---------------------------------------------------
users = Table(
    "users",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("email", Text, nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column(
        "role",
        Text,
        nullable=False,
        server_default=text(f"'{DEFAULT_USER_ROLE}'"),
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
# FR-1: case-insensitive email uniqueness via a functional unique index.
users.append_constraint(
    CheckConstraint(users.c.role.in_(USER_ROLES), name="users_role_check")
)
Index("idx_users_email_lower", func.lower(users.c.email), unique=True)

# --- genres (SPEC-1 2.2) --------------------------------------------------
genres = Table(
    "genres",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False, unique=True),
)

# --- movies (SPEC-1 2.3) --------------------------------------------------
movies = Table(
    "movies",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=text("''")),
    Column("poster_url", Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
Index("idx_movies_title", movies.c.title)

# --- movie_genres (many-to-many, SPEC-1 2.4) ------------------------------
movie_genres = Table(
    "movie_genres",
    metadata,
    Column(
        "movie_id",
        BigInteger,
        ForeignKey("movies.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    ),
    Column(
        "genre_id",
        BigInteger,
        ForeignKey("genres.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    ),
)

# --- screens (SPEC-1 2.5) -------------------------------------------------
screens = Table(
    "screens",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False, unique=True),
    Column("rows", SmallInteger, nullable=False),
    Column("seats_per_row", SmallInteger, nullable=False),
)
screens.append_constraint(
    CheckConstraint(screens.c.rows > 0, name="screens_rows_positive")
)
screens.append_constraint(
    CheckConstraint(screens.c.seats_per_row > 0, name="screens_seats_per_row_positive")
)

# --- seats (SPEC-1 2.6) ---------------------------------------------------
seats = Table(
    "seats",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "screen_id",
        BigInteger,
        ForeignKey("screens.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("row_label", Text, nullable=False),
    Column("seat_number", SmallInteger, nullable=False),
    Column(
        "seat_type",
        Text,
        nullable=False,
        server_default=text(f"'{DEFAULT_SEAT_TYPE}'"),
    ),
)
seats.append_constraint(
    CheckConstraint(seats.c.seat_type.in_(SEAT_TYPES), name="seats_type_check")
)
seats.append_constraint(
    UniqueConstraint(
        seats.c.screen_id,
        seats.c.row_label,
        seats.c.seat_number,
        name="seats_unique_position",
    )
)

# --- showtimes (SPEC-1 2.7) -----------------------------------------------
showtimes = Table(
    "showtimes",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "movie_id",
        BigInteger,
        ForeignKey("movies.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "screen_id",
        BigInteger,
        ForeignKey("screens.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("price_cents", Integer, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
# FR-5: price must be strictly positive.
showtimes.append_constraint(
    CheckConstraint(showtimes.c.price_cents > 0, name="showtimes_price_positive")
)
Index("idx_showtimes_movie_date", showtimes.c.movie_id, showtimes.c.starts_at)
Index("idx_showtimes_date", showtimes.c.starts_at)

# --- reservations (SPEC-1 2.8) --------------------------------------------
reservations = Table(
    "reservations",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "showtime_id",
        BigInteger,
        ForeignKey("showtimes.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "status",
        Text,
        nullable=False,
        server_default=text(f"'{DEFAULT_RESERVATION_STATUS}'"),
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
)
reservations.append_constraint(
    CheckConstraint(
        reservations.c.status.in_(RESERVATION_STATUSES),
        name="reservations_status_check",
    )
)
# FR-7 (Amendment 1): redundant with the primary key on purpose — it exists
# only as the target of the composite foreign key on seat_reservations (2.9).
reservations.append_constraint(
    UniqueConstraint(
        reservations.c.id,
        reservations.c.showtime_id,
        name="reservations_id_showtime_unique",
    )
)
Index(
    "idx_reservations_user",
    reservations.c.user_id,
    reservations.c.created_at.desc(),
)
Index("idx_reservations_showtime", reservations.c.showtime_id, reservations.c.status)

# --- seat_reservations (SPEC-1 2.9) ---------------------------------------
seat_reservations = Table(
    "seat_reservations",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("reservation_id", BigInteger, nullable=False),
    Column(
        "showtime_id",
        BigInteger,
        ForeignKey("showtimes.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "seat_id",
        BigInteger,
        ForeignKey("seats.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "status",
        Text,
        nullable=False,
        server_default=text(f"'{DEFAULT_RESERVATION_STATUS}'"),
    ),
    # FR-7 (Amendment 1): composite FK replaces the former single-column
    # reservation_id FK. It forces the denormalized showtime_id to agree with
    # its parent reservation's showtime_id and cascades on reservation delete.
    ForeignKeyConstraint(
        ["reservation_id", "showtime_id"],
        ["reservations.id", "reservations.showtime_id"],
        ondelete="CASCADE",
        name="seat_reservations_reservation_showtime_fk",
    ),
)
seat_reservations.append_constraint(
    CheckConstraint(
        seat_reservations.c.status.in_(RESERVATION_STATUSES),
        name="seat_reservations_status_check",
    )
)
# FR-2 / FR-3: at most one ACTIVE row per (showtime_id, seat_id); terminal
# rows (cancelled/expired) are excluded by the WHERE clause so they never
# block a new hold.
Index(
    "idx_seat_reservations_active_unique",
    seat_reservations.c.showtime_id,
    seat_reservations.c.seat_id,
    unique=True,
    postgresql_where=seat_reservations.c.status.in_(ACTIVE_RESERVATION_STATUSES),
)
Index("idx_seat_reservations_reservation", seat_reservations.c.reservation_id)
