# SPEC-5: Reservation Lifecycle for Users

Feature:      List own reservations, cancel a confirmed reservation
Priority:     P4 (User-Facing Lifecycle)
Status:       See SPEC-5.status — this line is for human reference only
Dependencies: SPEC-4 (reservations module, statuses, hold expiry rules),
              SPEC-3 (showtimes, movies, screens), SPEC-2 (`get_current_user`)
Related Docs: APP-OVERVIEW.md (2.5, 2.7), steering/principles.md,
              steering/product.md, steering/conventions.md,
              specs/SPEC-1-database-schema.md, specs/SPEC-4-seat-reservation-engine.md

---

## 1. Overview

Small SPEC that completes the user side of the reservation lifecycle
(`held` → `confirmed` → `cancelled`):

1. `GET /reservations`: the caller's own reservations, newest first, with the
   showtime, movie and screen embedded, filterable by status and by
   upcoming/past, paginated.
2. `POST /reservations/{id}/cancel`: the owner cancels a **confirmed**
   reservation of a showtime that has not started. Its seats become available
   at once.

Cancelling a *held* reservation already exists as `release` (SPEC-4, D5).
Nothing here changes a SPEC-4 endpoint or response shape.

No schema change (SPEC-1 stays frozen), no new dependency, no new setting.
The new code lives in the existing `reservations` module.

Technology: FastAPI, Pydantic v2, SQLAlchemy Core (async), row locks.

---

## 2. Design

### 2.1 Decisions (approved by George 2026-10-28: all as written)

**D1 — Cancel is for confirmed reservations only.** A held reservation is
given up with `release` (SPEC-4). Calling cancel on any non-confirmed
reservation returns 409 `RESERVATION_NOT_CONFIRMED`.

**D2 — Cancel is not idempotent.** A second cancel returns 409
`RESERVATION_NOT_CONFIRMED`. (SPEC-4 made only confirm idempotent; a `cancelled`
row may also be a released hold, so "already cancelled" is not a safe 200.)

**D3 — Cancel window = until the showtime starts.** Exactly as in
`APP-OVERVIEW.md`: `starts_at <= now` is refused (409 `SHOWTIME_STARTED`), with no
extra buffer such as "one hour before". There is no payment, so there is no
refund logic; "no partial refunds" stays a non-goal. The whole reservation is
cancelled; cancelling single seats is not supported.

**D4 — List filters.** `status` (`held | confirmed | cancelled | expired`) uses
the **effective** status (a `held` row older than the hold window counts as
`expired`, as in SPEC-4), and `upcoming` (`true`: showtime starts after now;
`false`: it has started; omitted: both). Order: `created_at DESC, id DESC`.
Pagination as in SPEC-3 (`limit` default 20, 1-100; `offset` >= 0).

**D5 — New list item model.** The list needs showtime, movie and screen data,
but `ReservationOut` (SPEC-4) must not change (`product.md`: changing an
endpoint's response shape needs approval). So the list returns a new
`ReservationListItem` that contains the same fields as `ReservationOut` plus a
nested `showtime`.

**D6 — History is kept.** Cancelling never deletes rows; the reservation and
its seat rows stay with status `cancelled` and `cancelled_at` set (reporting
in SPEC-6 counts confirmed reservations only).

### 2.2 Module layout

No new package. Additions:

```
backend/src/reservations/
  rules.py      + effective_status(stored_status, created_at, now, hold_minutes)
                  (reuse if SPEC-4 already has it; one definition only)
  schemas.py    + ReservationListItem, ShowtimeSummary, ListParams
  service.py    + list_reservations, cancel
  router.py     + GET /reservations, POST /reservations/{id}/cancel
```

Service functions take an `AsyncConnection`, never commit, never open a
connection; `now` is passed in, as in SPEC-4.

### 2.3 Endpoints

| Method | Path | Auth | Result |
|---|---|---|---|
| GET | `/reservations?status=&upcoming=&limit=&offset=` | user | 200 page of `ReservationListItem` |
| POST | `/reservations/{reservation_id}/cancel` | owner | 200 `ReservationOut` |

`GET /reservations` returns only the caller's rows; there is no way to ask for
someone else's (no `user_id` parameter). Admin views are SPEC-6.

Page envelope as in SPEC-3: `{"items": [...], "total": N, "limit": L,
"offset": O}`, built with the shared `Page` / `PageParams` in `src/core`.

`ReservationListItem`: all fields of `ReservationOut` (`id, showtime_id, status
(effective), seats [{seat_id,row_label,seat_number}], total_cents, created_at,
expires_at, confirmed_at, cancelled_at`) plus
`showtime: {id, starts_at (UTC), price_cents, movie: {id, title, poster_url},
screen: {id, name}}`.

`status` that is not one of the four values, `upcoming` that is not a boolean,
or out-of-range `limit`/`offset` → 422 `VALIDATION_ERROR`.

### 2.4 Error codes

| Code | Status |
|---|---|
| `RESERVATION_NOT_FOUND` (SPEC-4) | 404 |
| `RESERVATION_NOT_CONFIRMED` (new) | 409 |
| `SHOWTIME_STARTED` (SPEC-3/4) | 409 |
| `VALIDATION_ERROR`, `UNAUTHENTICATED` | 422, 401 |

### 2.5 List algorithm

`list_reservations(conn, user_id, status, upcoming, limit, offset, now)`:

1. One query: `reservations JOIN showtimes JOIN movies JOIN screens WHERE
   reservations.user_id = :uid`, plus the filters below, `ORDER BY created_at
   DESC, id DESC LIMIT/OFFSET`. Uses `idx_reservations_user`.
2. Filters, with `cutoff = rules.hold_cutoff(now, HOLD_MINUTES)`:
   - `status=held` → `status = 'held' AND created_at > cutoff`
   - `status=expired` → `status = 'expired' OR (status = 'held' AND created_at <= cutoff)`
   - `status=confirmed|cancelled` → `status = :value`
   - `upcoming=true` → `showtimes.starts_at > now`; `false` → `starts_at <= now`.
3. A second query for the page's seats (`WHERE reservation_id IN (...)`, joined
   to `seats`), grouped in Python. No N+1.
4. A count query with the same filters for `total`.
5. Build items; `status` is the **effective** status via the single
   `rules.effective_status`; `total_cents = showtime.price_cents × seat count`;
   `expires_at` only for effective `held`.

Reads never write (SPEC-4 rule).

### 2.6 Cancel algorithm

`cancel(conn, user_id, reservation_id, now)`:

1. Load the reservation `FOR UPDATE`; missing **or** not owned by the user →
   404 `RESERVATION_NOT_FOUND` (identical, D4 of SPEC-4).
2. Status is not `confirmed` → 409 `RESERVATION_NOT_CONFIRMED`.
3. Load the showtime; `starts_at <= now` → 409 `SHOWTIME_STARTED`.
4. Set the reservation to `cancelled` (`cancelled_at = now`) FIRST, then its
   seat rows to `cancelled` (same lock order as confirm, release and lazy expiry
   in SPEC-4: reservation row, then seat rows).
5. Return `ReservationOut` (same model as confirm/release).

Why it is safe:

- Seats become available only when the seat rows leave the partial unique
  index (`status IN ('held','confirmed')`); that happens in the same
  transaction as the reservation update, so no state exists where the
  reservation is cancelled but its seats are still blocked, or the reverse
  (SPEC-4 FR-11 stays true).
- Two simultaneous cancels queue on the reservation row lock; the second sees
  `cancelled` and gets 409.
- A new hold of the same seat racing with the cancel either conflicts (the
  seat rows are still active) and gets 409, or runs after the commit and
  succeeds; the unique index makes two active rows impossible in every
  interleaving.

### 2.7 Authentication vs authorization

Authentication: `get_current_user`. Authorization: the ownership check inside
the service function (`conventions.md`), as in SPEC-4.

---

## 3. Edge Cases & Constraints

- A user with no reservations: 200 with an empty page.
- `starts_at == now`: not upcoming; cancel refused. `now + 1 s`: upcoming;
  cancel allowed.
- A held reservation older than the window is listed as `expired` and can be
  filtered with `status=expired`, even though the stored value is still `held`
  until the next hold's lazy expiry (SPEC-4).
- Cancelled reservations stay in the list (newest first), filterable with
  `status=cancelled`.
- A reservation of a deleted showtime cannot exist (SPEC-3 refuses deleting
  showtimes with reservations).
- Cancel of someone else's reservation looks exactly like cancel of an unknown
  id (404).
- Cancelling frees the seats immediately; the next `GET /showtimes/{id}/seats`
  shows them as `available`.

---

## 4. Configuration and dependency changes

None. No new settings, no new dependencies, no migration, no change to SPEC-1
or to any SPEC-4 endpoint or response shape.

---

## 5. Testing Requirements

Unit (no DB, no HTTP), `tests/unit/`:

1. `test_effective_status_derivation` — held fresh stays held; held at
   exactly created + HOLD becomes expired; confirmed, cancelled and stored
   expired are unchanged
2. `test_list_params_validation` — status values accepted/rejected,
   `upcoming` bool parsing, `limit` 0/1/100/101, `offset` -1/0

Integration (real Postgres via `TEST_DATABASE_URL`, httpx `ASGITransport`,
rolled-back connection per test unless stated),
`tests/integration/test_reservation_lifecycle_api.py`:

List:
3. `test_list_requires_authentication_returns_401`
4. `test_list_returns_only_own_reservations_newest_first`
5. `test_list_item_embeds_showtime_movie_screen_and_seats`
6. `test_list_status_filter_uses_effective_status` — fresh held, stale held
   (expired), confirmed, cancelled
7. `test_list_upcoming_filter_true_false_and_boundary` — `starts_at == now`
   counts as not upcoming
8. `test_list_pagination_total_and_bounds_return_422`
9. `test_list_is_empty_for_user_without_reservations`

Cancel:
10. `test_cancel_confirmed_upcoming_marks_reservation_and_seat_rows_cancelled`
    — also checks `cancelled_at` and that the reservation and every seat row
    have the same status
11. `test_cancelled_seats_can_be_held_again_immediately`
12. `test_cancel_held_reservation_returns_409_not_confirmed`
13. `test_cancel_cancelled_or_expired_returns_409_not_confirmed`
14. `test_cancel_started_showtime_returns_409_boundary` — service called with
    explicit `now`: `starts_at == now` refused, `starts_at == now + 1 s` allowed
15. `test_cancel_other_users_or_unknown_reservation_returns_404`
16. `test_cancel_requires_authentication_returns_401`
17. `test_cancel_leaves_other_reservations_of_same_showtime_untouched`
18. `test_cancel_keeps_rows_and_list_shows_cancelled_status`
19. `test_error_bodies_use_standard_shape_for_new_code`

Concurrency (committed connections via `committing_client`, each test creates
its own data and removes everything in `finally`):

20. `test_concurrent_double_cancel_exactly_one_succeeds` — two simultaneous
    cancels of one confirmed reservation: one 200, one 409
    `RESERVATION_NOT_CONFIRMED`
21. `test_concurrent_cancel_and_hold_of_same_seat_never_two_active_rows` —
    10 rounds, each with fresh data: a confirmed reservation is cancelled by
    its owner while another user holds the same seat; per round the hold is
    201 or 409, never 5xx; after the round at most one active row exists for
    the seat; if the hold was 201 the original reservation is `cancelled`

### 5.1 Concurrency verification gate

Tests 20-21 must pass **five consecutive runs** (as in SPEC-4 5.1); the
Implementation Status records the five results.

---

## 6. Acceptance Criteria (EARS)

  FR-1  WHEN an authenticated user lists reservations, THE SYSTEM SHALL return
        only that user's reservations; IF the caller is unauthenticated, THE
        SYSTEM SHALL answer 401.
  FR-2  THE SYSTEM SHALL return the list newest first, each item containing
        the reservation fields, its seats, the showtime, movie and screen, the
        effective status and `total_cents`.
  FR-3  WHEN `status` is given, THE SYSTEM SHALL filter by effective status
        (a `held` reservation past its expiry is `expired`); IF `status` or
        `upcoming` is invalid, THE SYSTEM SHALL answer 422 `VALIDATION_ERROR`.
  FR-4  WHEN `upcoming` is given, THE SYSTEM SHALL return only reservations
        whose showtime starts after now (`true`) or has started (`false`).
  FR-5  THE SYSTEM SHALL paginate the list with `limit` (1-100, default 20) and
        `offset` (>= 0), reject out-of-range values with 422, and report
        `total`.
  FR-6  WHEN the owner cancels a `confirmed` reservation of a showtime that has
        not started, THE SYSTEM SHALL set the reservation and all its seat rows
        to `cancelled`, set `cancelled_at`, make the seats available
        immediately, and return 200.
  FR-7  IF the reservation is not `confirmed`, THE SYSTEM SHALL answer 409
        `RESERVATION_NOT_CONFIRMED`; IF the showtime has started, THE SYSTEM
        SHALL answer 409 `SHOWTIME_STARTED`.
  FR-8  IF the reservation does not exist or belongs to another user, THE SYSTEM
        SHALL answer 404 `RESERVATION_NOT_FOUND`; IF the caller is
        unauthenticated, 401.
  FR-9  IF two cancels of the same reservation arrive simultaneously, THE
        SYSTEM SHALL accept exactly one; IF a cancel and a hold of the same
        seat arrive simultaneously, THE SYSTEM SHALL never create two active
        rows for that seat.
  FR-10 THE SYSTEM SHALL keep a reservation and all of its seat rows in the
        same status after a cancel.
  FR-11 THE SYSTEM SHALL keep cancelled reservations and their seat rows
        (no deletion).
  FR-12 THE SYSTEM SHALL return every error in the standard
        `{"error": {"code", "message"}}` shape and SHALL NOT change the
        request or response shape of any SPEC-4 endpoint.

### 6.1 Requirements Mapping

| Requirement | Design decision / section | Tests |
|---|---|---|
| FR-1 | 2.3, 2.7 | 3, 4, 9 |
| FR-2 | 2.3 `ReservationListItem`, D5, 2.5 | 4, 5 |
| FR-3 | D4, 2.5 filters | 2, 6 |
| FR-4 | D4, 2.5 filters | 7 |
| FR-5 | 2.3 page envelope | 2, 8 |
| FR-6 | 2.6, D1, D3 | 10, 11, 17 |
| FR-7 | 2.6 steps 2-3, D1, D2 | 12, 13, 14 |
| FR-8 | 2.6 step 1, 2.7 | 15, 16 |
| FR-9 | 2.6 "Why it is safe", SPEC-1 unique index | 20, 21 |
| FR-10 | 2.6 step 4 | 10 |
| FR-11 | D6 | 18 |
| FR-12 | 2.4, D5 | 19 |

### Equivalence Partitioning + Boundary Value Analysis

Cancel vs showtime start (FR-7): `starts_at = now` refused; `now + 1 s` allowed.
Reservation status for cancel (FR-6, FR-7): `confirmed` → allowed; `held`,
`cancelled`, `expired` (stored or effective) → 409.
Hold age for the `status` filter (FR-3), `HOLD_MINUTES` = 10: age 9:59 → `held`;
age 10:00 → `expired`.
`upcoming` boundary (FR-4): `starts_at = now` → past; `now + 1 s` → upcoming.
`limit`: 0 / 1 and 100 / 101. `offset`: -1 / 0.

---

## 7. Logging Requirements

- Cancel: `INFO` with event name, user id, reservation id, showtime id, seat count.
- Refusals (409, 404 on cancel): `INFO` with event name and user id.
- Listing is not logged per request (read, high volume).
- Unexpected exceptions: `ERROR` with traceback (global handler).
- Never log tokens or full request bodies.

---

## 8. Reuse/Dependency Check

No new dependency. Reused unchanged: `get_current_user`, `DbConnection`,
`ApiError`, `Page` / `PageParams` (SPEC-3), `rules.hold_cutoff` /
`is_hold_expired` (SPEC-4), the lock order and `ReservationOut` (SPEC-4), the
test builders and the committing client from earlier SPECs.

---

## 9. Non-Goals / Revisit

Out of scope in v1: cancelling single seats, refunds or payment reversal,
a cancel cut-off before the start (for example one hour), cancellation fees,
rebooking/changing seats, email notifications, deleting reservations from a
user's history, admin cancellation (SPEC-6 only reads).

Revisit if: a real payment provider is added (cancel must trigger a refund and
becomes a multi-step operation); the business wants a cut-off window (a new
setting and a changed D3).

---

## 10. DRY Check

- Effective-status logic: only `rules.effective_status`; used by SPEC-4 reads
  and by the list.
- Expiry cutoff: only `rules.hold_cutoff`; the list SQL uses the same value.
- Page envelope and params: the shared `Page` / `PageParams`.
- Status vocabulary: the single definition in `src/core/enums.py`.
- Ownership + lock order: same helper/pattern as confirm and release; do not
  write a fourth copy of "load reservation FOR UPDATE and check owner" if a
  helper already exists.

## 11. Single Responsibility Check

- `rules.py`: pure status/time logic (no DB, no HTTP).
- `service.py`: queries and transactions (no HTTP types).
- `schemas.py`: validation and response shape.
- `router.py`: parsing, dependencies, status codes only.

## 11.1 Layer & Dependency Check

Unchanged from SPEC-4: `reservations` -> `core`, `db`, `auth.dependencies`;
it reads `src.db.tables` directly and does not import `movies` or `showtimes`.

---

## Amendment Log

(none yet)

---

## Implementation Status

NOT STARTED
