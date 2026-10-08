# SPEC-1: Data Model & Relationships

Feature:      Database Schema & Models
Priority:     P1 (Foundation)
Status:       See SPEC-1.status — this line is for human reference only
Dependencies: None
Related Docs: APP-OVERVIEW.md, steering/tech-stack.md, steering/principles.md

---

## 1. Overview

Defines every table, relationship, constraint, and index for the Movie
Reservation System. This is the schema every later SPEC builds on — in
particular, the `seat_reservations` uniqueness constraint defined here is
what SPEC-4 relies on to make seat-holding safe under concurrency.

Technology: PostgreSQL 16
Access layer: SQLAlchemy Core (`Table` objects, no ORM models/relationships)
Migrations: Alembic

---

## 2. Database Schema

### 2.1 users

```sql
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_role_check CHECK (role IN ('user', 'admin'))
);
CREATE UNIQUE INDEX idx_users_email_lower ON users (LOWER(email));
```

Validation rules (enforced in the `auth` module, not the DB, since they
depend on formats/policy that may change without a migration):
- Email must match a standard email pattern
- Password: minimum 8 characters (checked before hashing; only the hash is
  stored)
- Email uniqueness is case-insensitive (enforced by the DB via the
  lowercase functional index — this one IS a DB-level rule, since it's
  structural, not policy)

### 2.2 genres

```sql
CREATE TABLE genres (
    id   BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
```

### 2.3 movies

```sql
CREATE TABLE movies (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    poster_url  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_movies_title ON movies (title);
```

### 2.4 movie_genres (many-to-many)

```sql
CREATE TABLE movie_genres (
    movie_id BIGINT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    genre_id BIGINT NOT NULL REFERENCES genres(id) ON DELETE RESTRICT,
    PRIMARY KEY (movie_id, genre_id)
);
```

Cascade reasoning: deleting a movie should clean up its genre links
(nothing else references this join row); deleting a genre that's still in
use is blocked (RESTRICT) so an admin can't silently orphan a movie's
categorization — they must recategorize first.

### 2.5 screens

```sql
CREATE TABLE screens (
    id             BIGSERIAL PRIMARY KEY,
    name           TEXT UNIQUE NOT NULL,
    rows           SMALLINT NOT NULL,
    seats_per_row  SMALLINT NOT NULL,
    CONSTRAINT screens_rows_positive CHECK (rows > 0),
    CONSTRAINT screens_seats_per_row_positive CHECK (seats_per_row > 0)
);
```

### 2.6 seats

```sql
CREATE TABLE seats (
    id          BIGSERIAL PRIMARY KEY,
    screen_id   BIGINT NOT NULL REFERENCES screens(id) ON DELETE CASCADE,
    row_label   TEXT NOT NULL,        -- 'A', 'B', ...
    seat_number SMALLINT NOT NULL,    -- 1, 2, ...
    seat_type   TEXT NOT NULL DEFAULT 'standard',
    CONSTRAINT seats_type_check CHECK (seat_type IN ('standard', 'accessible')),
    CONSTRAINT seats_unique_position UNIQUE (screen_id, row_label, seat_number)
);
```

Cascade reasoning: a screen is only ever deleted as an admin correction
before it has any showtimes (enforced by 2.7's RESTRICT); once that's true,
cascading its seats is safe — there's nothing else to orphan.

### 2.7 showtimes

```sql
CREATE TABLE showtimes (
    id          BIGSERIAL PRIMARY KEY,
    movie_id    BIGINT NOT NULL REFERENCES movies(id) ON DELETE RESTRICT,
    screen_id   BIGINT NOT NULL REFERENCES screens(id) ON DELETE RESTRICT,
    starts_at   TIMESTAMPTZ NOT NULL,
    price_cents INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT showtimes_price_positive CHECK (price_cents > 0)
);
CREATE INDEX idx_showtimes_movie_date ON showtimes (movie_id, starts_at);
CREATE INDEX idx_showtimes_date ON showtimes (starts_at);
```

Cascade reasoning: RESTRICT on both `movie_id` and `screen_id` — deleting a
movie or screen that has showtimes (past or future) would silently orphan
reservation history. An admin must cancel/reassign showtimes first (out of
scope for v1's delete flow — see SPEC-3 edge cases: deleting a movie with
any showtimes is simply blocked, not offered as a cascading action).

No overlap constraint on `(screen_id, starts_at)` in the database in v1 —
preventing two showtimes overlapping on the same screen is a scheduling
rule enforced in the `showtimes` module at creation time (see SPEC-3),
not a DB constraint, because "overlap" requires knowing the movie's
runtime + a cleanup buffer, which isn't data this table alone has.

### 2.8 reservations

```sql
CREATE TABLE reservations (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    showtime_id  BIGINT NOT NULL REFERENCES showtimes(id) ON DELETE RESTRICT,
    status       TEXT NOT NULL DEFAULT 'held',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    CONSTRAINT reservations_status_check
        CHECK (status IN ('held', 'confirmed', 'cancelled', 'expired')),
    -- Redundant with the primary key on purpose: it exists only so that
    -- seat_reservations can reference (id, showtime_id) as one composite
    -- foreign key (see 2.9 and FR-7).
    CONSTRAINT reservations_id_showtime_unique UNIQUE (id, showtime_id)
);
CREATE INDEX idx_reservations_user ON reservations (user_id, created_at DESC);
CREATE INDEX idx_reservations_showtime ON reservations (showtime_id, status);
```

Cascade reasoning: RESTRICT everywhere — a reservation is a historical
record (for reporting/revenue) and must never disappear because a user or
showtime row was removed. In practice `users` and `showtimes` are not
deleted once reservations reference them (see `principles.md`: reporting
needs one consistent source of truth).

### 2.9 seat_reservations

This is the table that makes seat-locking safe. One row per seat held or
confirmed within a reservation.

```sql
CREATE TABLE seat_reservations (
    id             BIGSERIAL PRIMARY KEY,
    reservation_id BIGINT NOT NULL,
    showtime_id    BIGINT NOT NULL REFERENCES showtimes(id) ON DELETE RESTRICT,
    seat_id        BIGINT NOT NULL REFERENCES seats(id) ON DELETE RESTRICT,
    status         TEXT NOT NULL DEFAULT 'held',
    CONSTRAINT seat_reservations_status_check
        CHECK (status IN ('held', 'confirmed', 'cancelled', 'expired')),
    -- Composite FK: the denormalized showtime_id can never disagree with
    -- the parent reservation's showtime_id (FR-7). Replaces the former
    -- single-column reservation_id FK (Amendment 1).
    CONSTRAINT seat_reservations_reservation_showtime_fk
        FOREIGN KEY (reservation_id, showtime_id)
        REFERENCES reservations (id, showtime_id) ON DELETE CASCADE
);

-- The core correctness guarantee: only one ACTIVE (held or confirmed) row
-- may exist for a given seat on a given showtime, at the database level.
CREATE UNIQUE INDEX idx_seat_reservations_active_unique
    ON seat_reservations (showtime_id, seat_id)
    WHERE status IN ('held', 'confirmed');

CREATE INDEX idx_seat_reservations_reservation ON seat_reservations (reservation_id);
```

`showtime_id` is denormalized onto this table (also derivable via
`reservation_id → reservations.showtime_id`) specifically so the partial
unique index above can be a single-table constraint. This is the one
deliberate denormalization in the schema — flagged here per the DRY check
(Section 9) rather than left implicit. The composite foreign key above is
what keeps the duplicate honest: the database itself rejects any row whose
`showtime_id` differs from its parent reservation's, so the uniqueness
guarantee cannot be bypassed by a mislabeled row (FR-7).

Not enforced by the database (deliberately, handled in SPEC-4): that
`seat_id` belongs to the screen the showtime is scheduled on. Doing so
would need `screen_id` copied onto more tables; the `reservations` module
checks it inside the hold transaction instead.

Cascade reasoning: `ON DELETE CASCADE` from `reservations` — a
`seat_reservations` row has no independent meaning once its parent
reservation is gone (reservations are RESTRICT-protected from deletion in
practice, so this cascade is mostly theoretical, but it's the semantically
correct rule if it ever fires, e.g. in a test-data teardown).

---

## 3. Relationships

```
users        (1) → (many) reservations
movies       (1) → (many) showtimes
movies       (many) ↔ (many) genres           [movie_genres]
screens      (1) → (many) seats
screens      (1) → (many) showtimes
showtimes    (1) → (many) reservations
showtimes    (1) → (many) seat_reservations   [denormalized FK, see 2.9]
seats        (1) → (many) seat_reservations
reservations (1) → (many) seat_reservations
```

Cascade Rules Summary:
| Parent deleted | Child | Rule | Why |
|---|---|---|---|
| movies | movie_genres | CASCADE | join row has no independent meaning |
| genres | movie_genres | RESTRICT | force recategorization, don't silently orphan |
| screens | seats | CASCADE | only reachable once no showtimes reference the screen |
| movies | showtimes | RESTRICT | protect reservation history |
| screens | showtimes | RESTRICT | protect reservation history |
| users | reservations | RESTRICT | protect reporting history |
| showtimes | reservations | RESTRICT | protect reporting history |
| reservations | seat_reservations | CASCADE | no independent meaning |
| showtimes/seats | seat_reservations | RESTRICT | protect reporting history |

---

## 4. Edge Cases & Constraints

- A seat cannot be held/confirmed twice for the same showtime — enforced
  by the partial unique index in 2.9 (this is the load-bearing constraint
  of the whole project; see `principles.md`).
- `price_cents` and any future money column: integer, never float/numeric
  with implied decimal drift — `principles.md`.
- A showtime's `starts_at` must be in the future at creation time (checked
  in the `showtimes` module, not the DB, since "future" is relative to
  `now()` at insert time, not a static constraint Postgres can enforce
  declaratively across time).
- Deleting a movie or screen with any showtimes is blocked at the DB layer
  (RESTRICT) as the backstop; the `movies`/`showtimes` modules also check
  this before attempting the delete, to return a clean `409 CONFLICT`
  instead of surfacing a raw DB error to the API caller.
- Two showtimes must not overlap on the same screen — application-layer
  rule (see 2.7), not a DB constraint.

---

## 5. Testing Requirements

Required tests (integration, against a real test Postgres instance):

1. `test_create_user_and_unique_email_case_insensitive`
2. `test_users_role_check_rejects_invalid_role`
3. `test_movie_genre_many_to_many`
4. `test_delete_genre_in_use_is_restricted`
5. `test_delete_movie_with_showtimes_is_restricted`
6. `test_delete_screen_with_showtimes_is_restricted`
7. `test_seat_unique_position_per_screen`
8. `test_showtime_price_must_be_positive`
9. `test_seat_reservation_unique_active_constraint` — insert two `held`
   rows for the same `(showtime_id, seat_id)` → second insert raises a DB
   integrity error
10. `test_seat_reservation_allows_new_hold_after_prior_cancelled` — a
    `cancelled` row for the same seat/showtime does NOT block a new `held`
    row (proves the partial index's `WHERE` clause is correct)
11. `test_reservation_cascade_deletes_seat_reservations`
12. `test_migration_upgrade_and_downgrade` — Alembic `upgrade head` then
    `downgrade base` runs cleanly
13. `test_seat_reservation_showtime_must_match_reservation` — with a
    reservation for showtime A, inserting a `seat_reservations` row for
    that reservation but with showtime B's id raises a DB integrity error;
    the same insert with showtime A's id succeeds (FR-7)

---

## 6. Acceptance Criteria (EARS)

  FR-1  THE SYSTEM SHALL reject creation of a second `users` row with the
        same email, differing only in case.
  FR-2  IF two requests attempt to insert an active (`held` or
        `confirmed`) `seat_reservations` row for the same `(showtime_id,
        seat_id)`, THE SYSTEM SHALL allow exactly one to succeed and
        reject the other with a database integrity error.
  FR-3  WHEN a `seat_reservations` row's status is `cancelled` or
        `expired`, THE SYSTEM SHALL allow a new active row to be created
        for that same seat and showtime.
  FR-4  IF an admin attempts to delete a movie or screen that has any
        showtimes, THE SYSTEM SHALL reject the deletion at the database
        level (RESTRICT).
  FR-5  WHERE a showtime's `price_cents` is being inserted or updated,
        THE SYSTEM SHALL reject any value <= 0.
  FR-6  THE SYSTEM SHALL cascade-delete `seat_reservations` rows when
        their parent `reservations` row is deleted.
  FR-7  IF a `seat_reservations` row's `showtime_id` differs from the
        `showtime_id` of the reservation it belongs to, THE SYSTEM SHALL
        reject the row with a database integrity error.

### 6.1 Requirements Mapping

| Requirement | Design decision / section |
|---|---|
| FR-1 | `idx_users_email_lower` functional unique index (2.1) |
| FR-2 | `idx_seat_reservations_active_unique` partial unique index (2.9) |
| FR-3 | Partial index `WHERE status IN ('held','confirmed')` excludes terminal states (2.9) |
| FR-4 | `showtimes.movie_id` / `showtimes.screen_id` RESTRICT FKs (2.7) |
| FR-5 | `showtimes_price_positive` CHECK constraint (2.7) |
| FR-6 | `seat_reservations_reservation_showtime_fk` composite FK, `ON DELETE CASCADE` (2.9) |
| FR-7 | `reservations_id_showtime_unique` (2.8) + `seat_reservations_reservation_showtime_fk` (2.9) |

### Equivalence Partitioning + Boundary Value Analysis

For FR-5 (`price_cents`):
```
Partition 1: price_cents <= 0   (invalid)
Partition 2: price_cents > 0    (valid)

Boundary tests: price_cents = -1, 0, 1
```

For FR-2/FR-3 (seat status transitions), the meaningful "boundary" isn't
numeric but state-based — tests 9 and 10 in Section 5 cover both sides of
that boundary (an active status blocks; a terminal status doesn't).

---

## 7. Logging Requirements

- Every failed unique/check constraint violation surfaced to the API layer
  is logged at `INFO` (expected, user-facing conflict — e.g. "email
  already registered", "seat already taken"), not `ERROR`.
- Any other database integrity error (one not mapped to a known user-facing
  case) is logged at `ERROR` with the full exception, since it indicates
  either a schema/application mismatch or a genuinely unexpected state.
- Migration runs (`alembic upgrade`/`downgrade`) log at `INFO`: which
  revision, start/end timestamp — needed to answer "when did this schema
  change go out" months later.

---

## 8. Reuse/Dependency Check

New dependencies introduced by this spec: `sqlalchemy` (Core only),
`alembic`, `psycopg` (Postgres driver), `pytest`, `pytest-asyncio`,
no `testcontainers` (decided: tests use a plain PostgreSQL server at
`TEST_DATABASE_URL` — hosted Neon in development, a service container in CI; see
`steering/tech-stack.md`).

All are actively maintained, widely used, permissively licensed (MIT/BSD),
and each solves a problem substantial enough to justify the dependency
(schema definition + migrations + async Postgres access + testing) — none
of these would be reasonable to hand-roll.

---

## 9. DRY Check

- `price_cents`/integer-cents-for-money is a rule stated once in
  `principles.md` and applied here; SPEC-3 (showtime creation) and SPEC-6
  (revenue reporting) must reference this same column/convention, not
  introduce a second money representation.
- The `showtime_id` denormalization on `seat_reservations` (Section 2.9) is
  the one intentional duplication in this schema — called out explicitly
  so it isn't mistaken for an oversight, and so no later SPEC "fixes" it
  by removing the column (which would break the uniqueness constraint).
  The composite FK (2.9) guards the copy against drifting from its source.
- Status enums (`held`/`confirmed`/`cancelled`/`expired`) are duplicated
  across `reservations.status` and `seat_reservations.status` by necessity
  (a reservation can have a mix of seat-level states during partial
  operations is NOT allowed in v1 — see SPEC-4 for the rule that keeps
  these in sync) — the shared vocabulary should be defined once as a
  Python `Literal`/enum in `backend/src/core/` and imported by both modules, not
  restated as a string literal in multiple places.

---

## 10. Single Responsibility Check

No class/module boundary is introduced by this spec (it's pure schema —
`Table` objects, not classes with behavior). N/A for SRP; the Layer &
Dependency Check below is the relevant one for a schema-only spec.

## 10.1 Layer & Dependency Check

All business rules that are *not* purely structural (email format, password
policy, "showtime must be in the future", overlap checking) are explicitly
kept OUT of this schema and pushed to their owning module (`auth`,
`showtimes`) per `conventions.md`. The schema only encodes what must be
true regardless of which module touches the data — uniqueness, referential
integrity, non-negative money, valid enum values. This is the dividing
line used throughout: **if breaking the rule would corrupt the database
regardless of which code path caused it, it's a DB constraint; if it's
about acceptable business timing/policy, it's application logic.**

---

## Amendment Log

**Amendment 1 (2026-10-06) — found while reviewing PROMPT 2 output, before
any migration existed.**
Problem: `seat_reservations.showtime_id` is a deliberate copy of
`reservations.showtime_id`, but nothing forced the two to agree. A
mislabeled row could sit under a different showtime and slip past the
active-seat unique index, defeating the project's core guarantee at the
database level.
Change: add `UNIQUE (id, showtime_id)` on `reservations`; replace the
single-column `reservation_id` FK on `seat_reservations` with the composite
FK `(reservation_id, showtime_id)`. New FR-7 and test 13.
Impact: schema only, no behavior change for correct code. Requires
re-approval (SPEC-1.status reset to `planning`).

---

## Implementation Status

STATUS: IMPLEMENTED AND VERIFIED (2026-10-08)

Delivered (all under `backend/`): `pyproject.toml`, `src/core/{config,enums}.py`,
`src/db/{engine,tables,seed}.py`, `alembic.ini`, `migrations/env.py`,
`migrations/versions/0001_initial_schema.py`, `.env.example`,
`tests/conftest.py`, `tests/integration/test_schema.py`.

Verification with Evidence (against hosted Neon, database `movie_reservation_test`):

```
Claim:     Two active holds for the same seat/showtime cannot coexist, and a
           cancelled hold does not block a new one (FR-2, FR-3)
Command:   pytest tests/integration/test_schema.py -k "seat_reservation_unique_active or allows_new_hold_after_prior_cancelled" -v
Exit code: 0
Summary:   2 passed, 11 deselected
Verdict:   PASS

Claim:     The full schema suite proves FR-1 through FR-7 (13 tests)
Command:   python -m pytest tests/integration/test_schema.py -v
Exit code: 0
Summary:   13 passed, 3 warnings (alembic prepend_sys_path deprecation)
Verdict:   PASS

Claim:     Code quality gates are clean
Command:   ruff format --check src tests; ruff check src tests; mypy --strict src tests
Exit code: 0 / 0 / 0
Summary:   7 files formatted; all checks passed; no mypy issues in 7 files
Verdict:   PASS

Claim:     Seed is idempotent and produces the expected rows on the dev DB
Command:   python -m src.db.seed (twice), then count of screens/seats/users
Exit code: 0
Summary:   (2, 152, 1)
Verdict:   PASS
```

Deviations from the original plan (all documented before code):
- Amendment 1 (composite FK) — PROMPT-1b.
- passlib replaced by direct bcrypt; `sqlalchemy[asyncio]` — PROMPT-4b.
- Windows event-loop fix in the seed entry point — PROMPT-4c.
- No Docker/testcontainers: hosted Neon for dev and test; GitHub Actions
  postgres:16 service container planned for CI (tests are sync SQLAlchemy).

Known follow-ups (not blockers):
- `alembic.ini`: add `path_separator = os` to silence the DeprecationWarning.
- Dev/test run on Neon PostgreSQL 18, CI on postgres:16 — keep the schema free
  of version-specific features.
- CI workflow (GitHub Actions) not yet written.
- Move DB fixtures to `tests/integration/conftest.py` when DB-less
  `tests/unit/` tests are added.
