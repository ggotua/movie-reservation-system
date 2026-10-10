# Movie Reservation System — Application Overview

**Version:** 1.0
**Date:** 2026-09-22
**Status:** Planning
**Rigor tier:** Formal (auth + money-adjacent revenue reporting + data
integrity under concurrency — see `sdd_workflow_v2.txt` Stage 0)

---

## 1. Executive Summary

A backend system for a movie reservation service. Users sign up, log in,
browse movies and showtimes, reserve specific seats, and manage their own
reservations. Admins manage the movie catalog and showtimes, promote users
to admin, and view reporting on reservations, capacity, and revenue.

**Target Users:** Individuals booking movie tickets (regular users); a
small admin/staff group managing the catalog and reservations.
**Platform:** Backend API only (FastAPI), consumed via HTTP/JSON. No
frontend in scope beyond what's needed to exercise the API.
**Tech Stack:** Python 3.13, FastAPI, PostgreSQL, SQLAlchemy Core, Alembic,
JWT auth — see `docs/steering/tech-stack.md` for full rationale.

---

## 2. Core Features (MVP)

### 2.1 User Authentication & Authorization
- Sign up (email + password), log in (JWT issuance)
- Roles: `user` (default), `admin`
- Seed data creates the first admin account
- Admins can promote another user to admin
- Every route distinguishes authentication (valid token) from
  authorization (role + resource ownership) — see `principles.md`

### 2.2 Movie Management (admin)
- Add / update / delete movies
- Each movie: title, description, poster image (URL), genre(s)
- Deleting a movie with future showtimes is restricted, not cascaded (see
  SPEC-1 edge cases)

### 2.3 Showtime Management (admin)
- Create showtimes for a movie: date/time, screen, price (cents)
- A screen has a fixed seat layout (rows × seats, seeded per screen)

### 2.4 Browsing (public/user)
- List movies + showtimes for a given date
- View seat map for a specific showtime (available vs. held vs. taken)

### 2.5 Reservation Management (user)
- Select and hold one or more seats for a showtime (see 2.7 for the
  hold/confirm lifecycle)
- Confirm a held reservation (stubbed "payment" step)
- View own reservations
- Cancel own reservation — only if the showtime hasn't started yet

### 2.6 Reporting (admin)
- All reservations, filterable by movie/showtime/date
- Capacity: seats sold vs. total, per showtime
- Revenue: total and by movie/date range

### 2.7 Reservation Lifecycle (the core concurrency-safety feature)
A reservation moves through: `held` → `confirmed` → (`cancelled` |
`expired`).
- **Hold**: user selects seats; a row is inserted per seat with a unique
  constraint on `(showtime_id, seat_id)` for non-terminal states, inside a
  DB transaction — this is what makes two users racing for the same seat
  safe (see SPEC-4).
- **Expiry**: a hold that isn't confirmed within a fixed window
  (configurable, default short) is treated as expired and the seat becomes
  available again — checked lazily (on next read/write touching that seat)
  rather than requiring a background worker, to keep infrastructure minimal
  at this scale.
- **Confirm**: stubbed — no real payment gateway; confirming just
  transitions `held` → `confirmed`. This is the seam a real payment
  integration would plug into later (see `tech-stack.md`, Revisit If).
- **Cancel**: only `confirmed` reservations for a showtime that hasn't
  started yet can be cancelled by their owner.

### 2.8 Non-Goals

See `docs/steering/product.md` for the full list. Restated here for
visibility: no real payment processing, no multi-theater support, no
notifications, no seat-map visual editor, no waitlisting, no partial
refunds, no dynamic per-seat pricing.

### 2.9 Boundaries

See `docs/steering/product.md`. Summary: schema changes and new
dependencies need sign-off after a SPEC is approved; seat-uniqueness and
integer-cents money are non-negotiable.

---

## 3. System Architecture

```
Client (HTTP/JSON)
     ↓
FastAPI app (route layer — thin, parses/validates, calls module functions)
     ↓
Modules: auth | movies | showtimes | reservations | reporting
     ↓
SQLAlchemy Core (explicit queries, no ORM relationship magic)
     ↓
PostgreSQL (constraints enforce seat-uniqueness, cascades, referential integrity)
```

### 3.1 Architecture Decision

**Chosen:** Modular monolith — one FastAPI codebase, one Postgres database,
clear module boundaries (`auth`, `movies`, `showtimes`, `reservations`,
`reporting`) each exposing plain functions, not classes.

**Alternatives considered:**
- *Strict layered monolith* (explicit domain/application/infrastructure
  split) — more upfront boilerplate than this project's scope justifies;
  the modular-monolith module boundary already keeps business rules out of
  route handlers and out of table definitions, which is the actual risk
  this would guard against.
- *API-first with a formal versioned contract* — the "multiple future
  frontends" concern that justifies this doesn't apply here (no mobile
  app or second client is planned); would add ceremony with no near-term
  payoff.

**Reason:** Solo build, learning-focused, single deployable service, no
near-term need for independent scaling or deployment of separate
components. A modular monolith gets the main benefit (business logic
separated from framework/DB) without the overhead of physically separate
services.

**Revisit if:** a second, independently-scaled client (e.g. a mobile app
needing offline sync, or a separately-deployed admin dashboard) becomes a
real requirement — not just a hypothetical one.

---

## 4. Data Model (High-Level)

```
users            (id, email, password_hash, role, created_at)
genres           (id, name)
movies           (id, title, description, poster_url, created_at)
movie_genres     (movie_id, genre_id)                          -- many-to-many
screens          (id, name, rows, seats_per_row)
seats            (id, screen_id, row_label, seat_number, seat_type)
showtimes        (id, movie_id, screen_id, starts_at, price_cents)
reservations     (id, user_id, showtime_id, status, created_at, confirmed_at, cancelled_at)
seat_reservations(id, reservation_id, showtime_id, seat_id, status)
    -- unique constraint on (showtime_id, seat_id) WHERE status IN ('held','confirmed')
```

Full schema, constraints, indexes, and validation rules are defined in
SPEC-1 (not duplicated here — this is the high-level shape only, per SDD
convention of keeping APP-OVERVIEW at the summary level).

---

## 5. User Workflows

### First-time user
1. Sign up
2. Browse movies/showtimes for a date
3. Select a showtime → view seat map → hold seats → confirm

### Regular usage
1. Log in
2. Browse and reserve, or view/cancel existing reservations

### Admin
1. Log in as admin
2. Add/update movies and showtimes
3. Review reporting: capacity and revenue by movie/date
4. Promote a user to admin if needed

---

## 6. Technical Requirements

**Performance:** seat-hold requests must resolve the race safely under
concurrent load for the same showtime — this is the one place performance
and correctness are the same requirement (see SPEC-4's concurrency test).

**Security:**
- Password hashing via bcrypt
- JWT with reasonable expiry; role + ownership checked per SPEC-2/2.9
- SQL injection prevented structurally (SQLAlchemy Core parameterized
  queries only — no raw string interpolation)

**Data integrity:** seat double-booking prevented at the database
constraint level, not application logic alone (see `principles.md`).

---

## 7. Development Phases (maps to SPEC ordering — see FEATURE-PLAN.md)

- **Phase 1 — Foundation:** data model (SPEC-1), auth (SPEC-2)
- **Phase 2 — Catalog & scheduling:** movie/showtime management (SPEC-3)
- **Phase 3 — Core business logic:** seat reservation engine (SPEC-4)
- **Phase 4 — User-facing lifecycle:** view/cancel reservations (SPEC-5)
- **Phase 5 — Admin reporting:** capacity/revenue (SPEC-6)

---

## 8. Future Versions (explicitly out of scope now)

- Real payment gateway integration
- Multi-theater support
- Email/SMS notifications
- Waitlisting for sold-out showtimes
- Dynamic/tiered seat pricing

---

## 9. Success Criteria

MVP is complete when:
- A user can sign up, log in, browse showtimes by date, hold seats,
  confirm a reservation, view their reservations, and cancel an upcoming
  one.
- An admin can manage movies/showtimes, promote users, and see accurate
  capacity/revenue reporting.
- Two concurrent requests for the same seat on the same showtime never
  both succeed (verified by a concurrency test, not just code review).

---

## 10. Related Documents

- SPEC-1: Data Model & Relationships ✅ IMPLEMENTED (2026-10-08)
- SPEC-2: Auth & Roles ✅ IMPLEMENTED (2026-10-10)
- SPEC-3: Movie & Showtime Management 📝 PLANNING (spec drafted, awaiting approval)
- SPEC-4: Seat Reservation Engine (hold/confirm/expire) ⏳ PENDING
- SPEC-5: Reservation Lifecycle for Users (view/cancel) ⏳ PENDING
- SPEC-6: Admin Reporting (capacity/revenue) ⏳ PENDING
