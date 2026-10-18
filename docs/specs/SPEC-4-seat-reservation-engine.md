# SPEC-4: Seat Reservation Engine

Feature:      Seat map, race-safe seat hold, lazy hold expiry, confirm (stubbed
              payment), release of a hold
Priority:     P3 (Core Business Logic)
Status:       See SPEC-4.status — this line is for human reference only
Dependencies: SPEC-1 (reservations, seat_reservations, partial unique index),
              SPEC-2 (`get_current_user`, `DbConnection`, `ApiError`),
              SPEC-3 (showtimes, screens, seats)
Related Docs: APP-OVERVIEW.md (2.5, 2.7), steering/principles.md,
              steering/product.md, steering/conventions.md,
              specs/SPEC-1-database-schema.md (2.8, 2.9, Amendment 1),
              specs/SPEC-3-movies-and-showtimes.md

---

## 1. Overview

This is the SPEC the whole project exists to practice. It delivers:

1. The seat map of a showtime (available / held / taken), public.
2. **Hold**: a user selects 1..N seats of a showtime; all seats are held
   together or none are. Two users can never hold the same seat.
3. **Lazy expiry**: a hold that is not confirmed within `HOLD_MINUTES` stops
   blocking its seats. No background worker (APP-OVERVIEW 2.7).
4. **Confirm**: the stubbed payment step, `held` → `confirmed`.
5. **Release**: the owner gives up a hold, `held` → `cancelled`.
6. Reading one's own reservation.

Correctness is enforced twice: by the database (SPEC-1 partial unique index
`idx_seat_reservations_active_unique`, the guarantee that cannot be bypassed)
and by the transaction design in Section 2.5 (so a conflict is a clean 409,
never a half-created reservation). The concurrency tests in Section 5 are the
proof; this SPEC has a concurrency verification gate (Section 5.1).

No schema change; **SPEC-1 stays frozen** (D2). No new dependency. Two new
settings.

Out of scope here (SPEC-5): listing own reservations, cancelling a
**confirmed** reservation. Out of scope (SPEC-6): all admin views.

Technology: FastAPI, Pydantic v2, SQLAlchemy Core (async), PostgreSQL
`INSERT ... ON CONFLICT DO NOTHING`, row locks.

---

## 2. Design

### 2.1 Decisions (need George's approval)

**D1 — Hold window.** `HOLD_MINUTES`, default **10**, allowed 1–60. Per
`product.md`, the expiry window is an "ask first" item; approving this SPEC
approves 10 minutes as the default.

**D2 — Expiry has no column.** A hold's expiry is *derived*:
`reservations.created_at + HOLD_MINUTES`. SPEC-1 has no `expires_at` column
and none is added (no schema change). Consequence: changing `HOLD_MINUTES`
changes the expiry of holds that already exist; acceptable at this scale.
(Alternative: add `expires_at`, a SPEC-1 amendment + migration. Not
recommended.)

**D3 — Anti-hoarding limits.** At most `MAX_SEATS_PER_HOLD` seats per hold
(default **8**, allowed 1–20), and at most **one active hold per user per
showtime** (409 `ACTIVE_HOLD_EXISTS`). Without the second rule, one user could
block a whole showtime with repeated holds. To change seats, the user releases
the hold (D5) or lets it expire.

**D4 — Ownership returns 404.** Reading, confirming or releasing someone
else's reservation returns 404 `RESERVATION_NOT_FOUND`, the same as a
nonexistent id, so ids cannot be probed. (Alternative: 403 `FORBIDDEN`.)

**D5 — Release is in this SPEC.** `POST /reservations/{id}/release` for a
`held` reservation only. Cancelling a *confirmed* reservation is SPEC-5.

**D6 — Seat map is public** and shows only the state of each seat, never who
holds it.

**D7 — Confirm is idempotent.** Confirming an already confirmed reservation
by its owner returns 200 with the current state, so a retried request after a
lost response is safe.

**D8 — Conflict message names the seats.** A failed hold returns 409
`SEAT_UNAVAILABLE`; the message lists the seat ids that were unavailable.

### 2.2 Module layout

```
backend/src/
  core/
    config.py      + HOLD_MINUTES, MAX_SEATS_PER_HOLD (validated)
    enums.py       reservation/seat status vocabulary defined ONCE here
                   (`held`, `confirmed`, `cancelled`, `expired`) — SPEC-1 DRY
  reservations/
    rules.py       pure functions: hold expiry, seat state derivation
    schemas.py     HoldRequest, ReservationOut, SeatMapOut, ...
    service.py     business rules over an AsyncConnection
    router.py      one router; all routes use `conn: DbConnection`
  main.py          includes the reservations router
```

Service functions take an `AsyncConnection`, never commit, never open a
connection. Time is passed in (`now`, tz-aware UTC) so tests control it, as in
SPEC-3. The router computes `now = datetime.now(UTC)` once per request.

### 2.3 Endpoints

| Method | Path | Auth | Result |
|---|---|---|---|
| GET | `/showtimes/{showtime_id}/seats` | none | 200 `SeatMapOut` / 404 `SHOWTIME_NOT_FOUND` |
| POST | `/reservations` | user | 201 `ReservationOut` (a hold) |
| GET | `/reservations/{reservation_id}` | owner | 200 `ReservationOut` / 404 |
| POST | `/reservations/{reservation_id}/confirm` | owner | 200 `ReservationOut` |
| POST | `/reservations/{reservation_id}/release` | owner | 200 `ReservationOut` |

All `/reservations` routes depend on `get_current_user` (anonymous 401).
Admins have no special access here (admin views are SPEC-6).

`HoldRequest`: `{"showtime_id": int, "seat_ids": [int, ...]}`,
`extra="forbid"`, `seat_ids` distinct ints, at least 1. The upper bound is
checked in the service against `MAX_SEATS_PER_HOLD` (422 `TOO_MANY_SEATS`).

`ReservationOut`: `id, showtime_id, status, seats: [{seat_id, row_label,
seat_number}] (ordered by row, number), total_cents, created_at,
expires_at (null unless status is held), confirmed_at (nullable),
cancelled_at (nullable)`.
`status` is the **effective** status: a `held` row past its expiry is
reported as `expired` (reads never write, 2.5).
`total_cents = showtime.price_cents × number of seats` (integer; computed,
not stored).

`SeatMapOut`: `showtime_id, screen: {id, name, rows, seats_per_row},
seats: [{seat_id, row_label, seat_number, seat_type, state}]` with `state` in
`available | held | taken`. `held` = an active, unexpired hold; `taken` =
confirmed. An expired hold shows as `available`.

### 2.4 Error codes

| Code | Status |
|---|---|
| `SHOWTIME_NOT_FOUND`, `SEAT_NOT_FOUND`, `RESERVATION_NOT_FOUND` | 404 |
| `SEAT_UNAVAILABLE`, `ACTIVE_HOLD_EXISTS` | 409 |
| `RESERVATION_EXPIRED`, `RESERVATION_NOT_HELD` | 409 |
| `SHOWTIME_STARTED` (code reused from SPEC-3) | 409 |
| `TOO_MANY_SEATS` | 422 |
| `VALIDATION_ERROR`, `UNAUTHENTICATED` | 422, 401 |

`SEAT_NOT_FOUND` is returned when any requested id does not exist **or**
belongs to a different screen than the showtime's. Constants live in one place
in the module.

### 2.5 The hold algorithm (the core of this SPEC)

`hold(conn, user, showtime_id, seat_ids, now)` runs inside the request
transaction:

1. **Showtime.** Load it (404). `starts_at <= now` → 409 `SHOWTIME_STARTED`.
2. **Count.** `len(seat_ids) > MAX_SEATS_PER_HOLD` → 422 `TOO_MANY_SEATS`.
3. **Seats.** One query: seats with `id IN :ids AND screen_id =
   showtime.screen_id`. Fewer rows than requested → 404 `SEAT_NOT_FOUND`
   (this is the check SPEC-1 2.9 delegated to this module).
4. **Serialize this user.** `SELECT id FROM users WHERE id = :uid FOR UPDATE`.
   Two simultaneous holds by the same user queue here, so rule D3 cannot be
   raced.
5. **Lazy expiry (writes).** Select the showtime's expired holds:
   `SELECT id FROM reservations WHERE showtime_id = :sid AND status = 'held'
   AND created_at <= :now - HOLD ORDER BY id FOR UPDATE`, then set those
   reservations **and all their seat rows** to `expired`, in the same
   transaction. Locking in `id` order prevents deadlocks between two
   concurrent holds. A reservation is expired as a whole: a reservation and its
   seat rows always have the same status (FR-11; SPEC-1 DRY note).
6. **One active hold per user per showtime.** If the user still has a
   `held` reservation for this showtime → 409 `ACTIVE_HOLD_EXISTS`.
7. **Insert, all or nothing** inside `begin_nested()` (SAVEPOINT):
   a. insert the `reservations` row (`held`, `created_at = now`);
   b. insert all `seat_reservations` rows in **ascending `seat_id` order** in
      one statement with
      `ON CONFLICT (showtime_id, seat_id) WHERE status IN ('held','confirmed')
      DO NOTHING RETURNING seat_id`;
   c. if fewer rows were returned than requested, the savepoint is rolled back
      (the reservation row disappears with it) and the request fails with 409
      `SEAT_UNAVAILABLE`, naming the unavailable seats (found by a follow-up
      query on the active rows);
   d. any `IntegrityError` unique violation that still escapes is mapped to the
      same 409 (defense in depth).
8. Return the `ReservationOut`.

Why this is safe:

- The partial unique index is the arbiter. Two transactions inserting the same
  `(showtime_id, seat_id)` cannot both succeed: the second one waits for the
  first to finish and then conflicts (or proceeds if the first rolled back).
- Ascending `seat_id` order gives every transaction the same lock order, so
  two holds with overlapping seat sets cannot deadlock.
- `created_at = now` uses the application clock (one value per request), the
  same clock as expiry checks, so there is one time source.
- A stale `held` row blocks the index until step 5 flips it to `expired`;
  step 5 runs before step 7 in the same transaction, so an expired hold never
  blocks a new hold on the seats of that showtime.

**Reads never write.** The seat map and `GET /reservations/{id}` compute
expiry from `created_at` and `now` and do not update rows.

### 2.6 Confirm

`confirm(conn, user, reservation_id, now)`:

1. Load the reservation `FOR UPDATE`; missing or not owned by the user → 404.
2. `confirmed` → return it unchanged (idempotent, D7).
3. `cancelled` or `expired` → 409 `RESERVATION_NOT_HELD`.
4. `held` but `created_at + HOLD <= now` → 409 `RESERVATION_EXPIRED`
   (nothing is written; the next hold's lazy expiry cleans it).
5. Showtime `starts_at <= now` → 409 `SHOWTIME_STARTED`.
6. Set the reservation to `confirmed` (`confirmed_at = now`) and its seat rows
   to `confirmed`, in that order (the same order as expiry and release, so
   these operations cannot deadlock).

Confirming does not change which rows are active, so the unique index is
untouched. There is no payment: this is the seam for a real payment later
(`tech-stack.md`, Revisit If).

### 2.7 Release

`release(conn, user, reservation_id, now)`: lock `FOR UPDATE`, owner check
(404), must be `held` and not expired, otherwise 409 `RESERVATION_NOT_HELD`
(an expired hold is already free). Set reservation and seat rows to
`cancelled` (`cancelled_at = now`). The seats are immediately available.

### 2.8 Authentication vs authorization

Authentication: `get_current_user` (SPEC-2). Authorization is the ownership
check **inside the service function** (`conventions.md`), so it holds even if
another module calls the function later. The seat map is public.

---

## 3. Edge Cases & Constraints

- Two users, same seat, same instant: exactly one 201, the rest 409.
- Overlapping multi-seat holds ({1,2,3} vs {3,4,5}): exactly one wins; the
  loser leaves **no** reservation row and no seat row.
- A hold at exactly `created_at + HOLD_MINUTES` is expired; one second earlier
  it still blocks (boundary, test 20).
- A user's own expired hold does not trigger `ACTIVE_HOLD_EXISTS` (expiry runs
  first).
- Holding seats on a showtime that already started: 409 `SHOWTIME_STARTED`.
- Expired reservations stay in the table (`expired`) for reporting.
- Releasing or confirming never touches another showtime's rows.
- Confirm of a stale hold whose seat was taken meanwhile: the stale hold is
  expired (the seat can only be re-held after expiry), so the answer is
  `RESERVATION_EXPIRED`, never a double booking.
- `price_cents × seats` is at most 100 000 000 × 20: no overflow.
- Changing `HOLD_MINUTES` later is not retroactive for stored data but does
  change the computed expiry of existing holds (D2).

---

## 4. Configuration and dependency changes

`core/config.py` adds (with defaults, in `.env.example`):

- `HOLD_MINUTES` (default 10): integer 1–60, otherwise the app refuses to start.
- `MAX_SEATS_PER_HOLD` (default 8): integer 1–20, otherwise the app refuses to
  start.

`core/enums.py` gets the single definition of the four statuses (check first
whether it already exists; do not create a second one). No new dependencies, no
migration, no change to SPEC-1.

---

## 5. Testing Requirements

Unit (no DB, no HTTP), `tests/unit/`:

1. `test_hold_expiry_boundary` — created + HOLD − 1 s: not expired; exactly
   created + HOLD: expired; + 1 s: expired
2. `test_hold_expires_at_adds_configured_minutes`
3. `test_seat_state_derivation` — no row → available; fresh held → held;
   stale held → available; confirmed → taken; cancelled/expired → available
4. `test_settings_reject_invalid_hold_minutes_and_max_seats` — hold 0, 1, 60,
   61; max seats 0, 1, 20, 21
5. `test_hold_schema_boundaries` — empty `seat_ids` rejected; 1 accepted;
   duplicates rejected; non-int and float ids rejected; extra fields rejected;
   `showtime_id` not an int rejected

Integration (real Postgres via `TEST_DATABASE_URL`, httpx `ASGITransport`,
rolled-back connection per test unless stated),
`tests/integration/test_reservations_api.py`:

Seat map:
6. `test_seat_map_is_public_and_lists_every_seat_of_the_screen`
7. `test_seat_map_shows_held_taken_and_treats_expired_hold_as_available`
8. `test_seat_map_unknown_showtime_returns_404`

Hold:
9. `test_hold_requires_authentication_returns_401`
10. `test_hold_creates_held_reservation_with_seats_and_total`
11. `test_hold_expires_at_is_created_plus_hold_minutes`
12. `test_hold_unknown_showtime_404_and_started_showtime_409`
13. `test_hold_seat_of_other_screen_or_unknown_returns_404_and_creates_nothing`
14. `test_hold_validation_failures_return_422` — empty, duplicates, extra field,
    string ids, `TOO_MANY_SEATS` at max+1 (and max accepted)
15. `test_hold_with_taken_seat_returns_409_all_or_nothing` — A holds {1,2}; B asks
    {2,3}: 409 naming seat 2, seat 3 still `available`, no B reservation or
    seat row exists
16. `test_hold_of_confirmed_seat_returns_409`
17. `test_hold_after_release_succeeds`
18. `test_hold_after_expiry_succeeds_and_marks_old_reservation_and_seats_expired`
19. `test_hold_one_second_before_expiry_blocked_and_at_expiry_allowed` (service
    called with explicit `now`)
20. `test_second_active_hold_same_user_same_showtime_returns_409` — other
    showtime allowed; allowed again after expiry or release
21. `test_lazy_expiry_does_not_touch_other_showtimes`

Confirm:
22. `test_confirm_owner_held_returns_confirmed_and_seats_become_taken`
23. `test_confirm_is_idempotent_for_owner`
24. `test_confirm_after_expiry_returns_409_expired_boundary` (1 s before ok, at
    expiry rejected)
25. `test_confirm_cancelled_or_expired_returns_409_not_held`
26. `test_confirm_on_started_showtime_returns_409`
27. `test_confirm_other_users_or_unknown_reservation_returns_404`
28. `test_hold_of_confirmed_seat_after_confirm_returns_409`

Release and read:
29. `test_release_held_marks_cancelled_and_frees_seats_immediately`
30. `test_release_confirmed_returns_409_and_other_users_404`
31. `test_get_reservation_owner_only_and_reports_effective_expired_status`
32. `test_total_cents_is_price_times_seat_count`
33. `test_error_bodies_use_standard_shape_for_new_codes`
34. `test_reservation_and_its_seat_rows_never_have_different_statuses` — runs a
    sequence of hold / confirm / release / expiry and checks, with SQL, that no
    reservation differs from any of its seat rows (FR-11)

Concurrency (committed connections via `committing_client`, each test creates
its own users/screen/showtime and removes everything in `finally`):

35. `test_concurrent_holds_of_one_seat_exactly_one_succeeds` — 20 different
    users, `asyncio.gather`: exactly one 201 and 19 × 409 `SEAT_UNAVAILABLE`;
    exactly one active `seat_reservations` row for the seat
36. `test_concurrent_overlapping_multi_seat_holds_never_partial_never_double` —
    holds {1,2,3}, {3,4,5}, {5,6,1}, {2,4,6} at once: each seat has at most one
    active row; every winner owns exactly its requested seats; every loser left
    no reservation row; no deadlock error
37. `test_concurrent_random_holds_keep_all_invariants` — 30 users, 12 seats,
    random subsets of 1-3 seats (fixed seed): per seat at most one active row;
    successes + failures = 30; reservations with seats = successes; no
    reservation without seats
38. `test_concurrent_confirm_of_stale_hold_and_new_hold_on_same_seat` — a
    reservation older than the window is created by SQL; its owner confirms
    while another user holds the same seat: confirm → 409 `RESERVATION_EXPIRED`,
    hold → 201, no deadlock, exactly one active row
39. `test_concurrent_double_hold_by_same_user_exactly_one_succeeds` — same user,
    two simultaneous holds on different seats: one 201, one 409
    `ACTIVE_HOLD_EXISTS`
40. `test_database_still_rejects_duplicate_active_row_when_service_is_bypassed`
    — two raw SQL inserts of the same `(showtime_id, seat_id)` active rows: the
    second raises `IntegrityError` (SQLSTATE 23505); defense in depth

### 5.1 Concurrency verification gate

Tests 35-40 must pass **five consecutive runs** (a loop of
`pytest -k "concurrent or still_rejects"`), not once. The Implementation
Status records the five results. A flaky concurrency test is a bug in the
code or the test, never ignored or retried silently.

---

## 6. Acceptance Criteria (EARS)

  FR-1  WHEN an authenticated user requests a hold for valid seats of a showtime
        that has not started, THE SYSTEM SHALL create one `held` reservation
        with one `held` seat row per seat, in one transaction, and return 201
        with the seats, `total_cents` and `expires_at`.
  FR-2  THE SYSTEM SHALL never allow two active (`held` or `confirmed`) seat
        rows for the same seat and showtime, including when requests arrive
        simultaneously; IF any requested seat is unavailable, THE SYSTEM SHALL
        answer 409 `SEAT_UNAVAILABLE` naming those seats and create no
        reservation and no seat row at all.
  FR-3  IF a requested seat does not exist or does not belong to the showtime's
        screen, THE SYSTEM SHALL answer 404 `SEAT_NOT_FOUND`; IF `seat_ids` is
        empty, duplicated, has a non-integer, or the request has extra fields,
        THE SYSTEM SHALL answer 422 `VALIDATION_ERROR`; IF more than
        `MAX_SEATS_PER_HOLD` seats are requested, THE SYSTEM SHALL answer 422
        `TOO_MANY_SEATS`.
  FR-4  IF the showtime does not exist, THE SYSTEM SHALL answer 404; IF it has
        started, THE SYSTEM SHALL refuse a hold or a confirmation with 409
        `SHOWTIME_STARTED`.
  FR-5  A hold SHALL stop blocking its seats at `created_at + HOLD_MINUTES`;
        WHEN a hold is requested on a showtime, THE SYSTEM SHALL first mark that
        showtime's expired holds and all their seat rows `expired` in the same
        transaction.
  FR-6  IF a user already has an unexpired `held` reservation for the showtime,
        THE SYSTEM SHALL answer 409 `ACTIVE_HOLD_EXISTS`, including when two such
        requests arrive simultaneously.
  FR-7  WHEN the owner confirms a `held`, unexpired reservation of a showtime
        that has not started, THE SYSTEM SHALL set the reservation and its seat
        rows to `confirmed` and return 200; confirming an already confirmed
        reservation SHALL return 200 unchanged; IF it is expired, THE SYSTEM
        SHALL answer 409 `RESERVATION_EXPIRED`; IF it is cancelled or expired,
        409 `RESERVATION_NOT_HELD`.
  FR-8  WHEN the owner releases an unexpired `held` reservation, THE SYSTEM
        SHALL set it and its seat rows to `cancelled`, making the seats
        available immediately; any other status SHALL give 409
        `RESERVATION_NOT_HELD`.
  FR-9  IF a reservation does not exist or belongs to another user, THE SYSTEM
        SHALL answer 404 `RESERVATION_NOT_FOUND` for read, confirm and release;
        IF the caller is unauthenticated, 401.
  FR-10 THE SYSTEM SHALL serve the seat map of a showtime publicly, showing each
        seat as `available`, `held` or `taken`, treating an expired hold as
        `available`, without writing to the database and without revealing who
        holds a seat.
  FR-11 THE SYSTEM SHALL keep a reservation and all of its seat rows in the
        same status at all times.
  FR-12 THE SYSTEM SHALL compute `total_cents` as the showtime's `price_cents`
        times the number of seats, in integer cents.
  FR-13 IF `HOLD_MINUTES` is outside 1-60 or `MAX_SEATS_PER_HOLD` outside 1-20,
        THE SYSTEM SHALL refuse to start.
  FR-14 THE SYSTEM SHALL return every error from these endpoints in the
        standard `{"error": {"code", "message"}}` shape.

### 6.1 Requirements Mapping

| Requirement | Design decision / section | Tests |
|---|---|---|
| FR-1 | 2.3, 2.5 steps 1-8 | 10, 11 |
| FR-2 | 2.5 step 7 (ON CONFLICT + savepoint), SPEC-1 unique index | 15, 16, 35, 36, 37, 40 |
| FR-3 | 2.5 steps 2-3, 2.4 | 5, 13, 14 |
| FR-4 | 2.5 step 1, 2.6 step 5 | 12, 26 |
| FR-5 | D1, D2, 2.5 step 5 | 1, 2, 3, 18, 19, 21 |
| FR-6 | D3, 2.5 steps 4 and 6 | 20, 39 |
| FR-7 | 2.6, D7 | 22, 23, 24, 25, 28, 38 |
| FR-8 | 2.7, D5 | 17, 29, 30 |
| FR-9 | D4, 2.8 | 9, 27, 30, 31 |
| FR-10 | D6, 2.5 "Reads never write" | 3, 6, 7, 8 |
| FR-11 | 2.5 step 5, 2.6 step 6, 2.7 | 18, 34 |
| FR-12 | 2.3 | 32 |
| FR-13 | Section 4 | 4 |
| FR-14 | 2.4 | 33 |

### Equivalence Partitioning + Boundary Value Analysis

Hold age vs window (FR-5, FR-7), `HOLD_MINUTES` = 10:
```
age < 10:00           active, blocks seats, confirmable   boundary: 9:59
age >= 10:00          expired, does not block             boundary: 10:00
```
Seats per hold (FR-3), max = 8:
```
0 seats               invalid (422)       boundary: 0 / 1
1 .. 8 seats          valid               boundary: 8 / 9
> 8 seats             TOO_MANY_SEATS (422)
```
`HOLD_MINUTES`: 0 / 1 and 60 / 61. `MAX_SEATS_PER_HOLD`: 0 / 1 and 20 / 21.
Showtime start vs now (FR-4): `starts_at = now` is started (hold refused);
`now + 1 s` allowed.
Seat selection (FR-2): all free; one taken (all-or-nothing); all taken;
overlap between two simultaneous requests (partial overlap); identical sets.

---

## 7. Logging Requirements

- Hold created, confirmed, released: `INFO` with event name, user id,
  reservation id, showtime id, seat count (no seat labels needed).
- Seat conflicts (`SEAT_UNAVAILABLE`, `ACTIVE_HOLD_EXISTS`) and expiry
  refusals: `INFO` with event name, user id, showtime id.
- Lazy expiry: `INFO` with showtime id and the number of reservations expired.
- Unexpected exceptions, including any database deadlock or serialization
  error: `ERROR` with traceback (global handler). A deadlock is a bug against
  this design and must be visible.
- Never log tokens, passwords or full request bodies.

---

## 8. Reuse/Dependency Check

No new dependency. Reused unchanged: `get_current_user`, `DbConnection`,
`ApiError`/`error_body`, the `begin_nested()` pattern, `SHOWTIME_STARTED`
constant semantics from SPEC-3, the SPEC-3 test helpers (`create_movie`,
`create_screen`, `create_showtime` builders) and the committing-client fixture.

SQLAlchemy Core's PostgreSQL `insert(...).on_conflict_do_nothing(
index_elements=..., index_where=...)` is part of the already declared
dependency.

---

## 9. Non-Goals / Revisit

Out of scope in v1: real payment, background expiry worker, seat price
differences, extending a hold, moving a hold to other seats, group/auto seat
assignment, waitlist, per-user daily limits, notifications, admin override of
holds, WebSocket seat-map updates (the client polls the seat map).

Revisit if: the stubbed payment is replaced by a real provider (confirm becomes
a two-step flow with a pending state and webhook); polling load becomes a
problem (push updates); holds must survive a `HOLD_MINUTES` change unchanged
(add `expires_at`, an amendment to SPEC-1).

---

## 10. DRY Check

- Status vocabulary: defined once in `core/enums.py`, used by SPEC-1 tables'
  Python side and by this module; no bare `'held'` strings in `reservations`.
- Expiry arithmetic: only in `reservations/rules.py` (`is_hold_expired`,
  `hold_expires_at`); the SQL cutoff is computed from the same function.
- Seat state derivation: one pure function used by the seat map.
- `HOLD_MINUTES` and `MAX_SEATS_PER_HOLD`: only from settings.
- Auth: only `get_current_user`; no second token parsing.

## 11. Single Responsibility Check

- `rules.py`: pure time/state logic (no DB, no HTTP).
- `service.py`: transactions and business rules over a connection (no HTTP
  types).
- `schemas.py`: validation and response shape.
- `router.py`: parsing, dependencies, status codes only.

## 11.1 Layer & Dependency Check

Allowed imports: `reservations` -> `core`, `db`, `auth.dependencies`.
`reservations` does **not** import `movies` or `showtimes`; it reads
`src.db.tables` directly (showtimes, screens, seats, users). `showtimes` and
`movies` do not import `reservations` (SPEC-3 already queries the
`reservations` table directly for its delete rule). `db` -> `core`.

---

## Amendment Log

(none yet)

---

## Implementation Status

NOT STARTED
