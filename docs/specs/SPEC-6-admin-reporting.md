# SPEC-6: Admin Reporting

Feature:      Admin view of all reservations, capacity per showtime, revenue by
              movie or by day
Priority:     P5 (Admin Reporting)
Status:       See SPEC-6.status — this line is for human reference only
Dependencies: SPEC-4 (reservations, statuses, hold expiry), SPEC-5 (effective
              status filter), SPEC-3 (showtimes, movies, screens, cinema
              timezone), SPEC-2 (`require_admin`)
Related Docs: APP-OVERVIEW.md (2.6), steering/principles.md,
              steering/product.md, steering/conventions.md,
              specs/SPEC-1-database-schema.md, specs/SPEC-3-movies-and-showtimes.md,
              specs/SPEC-4-seat-reservation-engine.md,
              specs/SPEC-5-reservation-lifecycle.md

---

## 1. Overview

The last backend SPEC. Three admin-only, **read-only** endpoints:

1. `GET /admin/reservations`: every user's reservations, filterable by movie,
   showtime, showtime-date range and status, newest first, paginated.
2. `GET /admin/reports/capacity`: per showtime, seats sold versus total
   (and held, available).
3. `GET /admin/reports/revenue`: total revenue and revenue grouped by movie or
   by day, over a date range.

Money is integer cents everywhere (`principles.md`). Revenue and capacity
come from the same facts (confirmed seat rows and `showtimes.price_cents`), so
they always reconcile; a test proves it (FR-7).

No schema change (SPEC-1 stays frozen), no new dependency, no new setting.
Reports never write to the database.

Technology: FastAPI, Pydantic v2, SQLAlchemy Core (async) aggregate queries.

---

## 2. Design

### 2.1 Decisions (approved by George 2026-10-29: all as written)

**D1 — What "revenue" means.** *Booked revenue*: the sum of
`showtimes.price_cents` over **confirmed** seat rows, attributed to the
**showtime's** date (not to the confirmation date), and including showtimes
in the future. Held, expired and cancelled seats earn nothing. The system has
no payment provider and no refunds, so a cancelled reservation simply stops
counting.

**D2 — Dates mean showtime calendar days.** `date_from` / `date_to` are
calendar days in `CINEMA_TIMEZONE` (same rule as SPEC-3), applied to
`showtimes.starts_at`, both ends **inclusive**.

**D3 — Capacity has four numbers.** For each showtime: `total_seats`
(seats of its screen), `sold_seats` (confirmed), `held_seats` (unexpired holds;
an expired hold is not counted), `available_seats = total - sold - held`.

**D4 — Routes.** `GET /admin/reservations`, `GET /admin/reports/capacity`,
`GET /admin/reports/revenue`, all behind `require_admin`.

**D5 — Admin reservation list shows the user.** Each item includes the owner's
`id` and `email` (admins only; nothing else about the user, never the hash).
No `user_id` filter in v1.

**D6 — Revenue grouping.** `group_by` is `movie` (default) or `day`. `day`
requires both dates and a range of at most 366 days. Only groups with at least
one sold ticket appear.

**D7 — Layering and one shared SQL definition.** `reporting` may import the
**pure** `rules` modules of `reservations` and `showtimes` (no DB, no HTTP) and
never their services, schemas or routers. The SQL condition "effective status"
(SPEC-5, D4) is needed by both SPEC-5 and this SPEC, so it moves to one shared
function in `src/db/reservation_filters.py`, and the SPEC-5 list is changed to
call it (no behavior change; the SPEC-5 tests prove it).

**D8 — No new indexes, no caching.** Existing indexes
(`idx_reservations_showtime`, `idx_showtimes_date`, ...) are enough at this
scale. Revisit if a report becomes slow (Section 9).

**D9 — No CSV/Excel export** in v1.

### 2.2 Module layout

```
backend/src/
  db/
    reservation_filters.py   effective_status_condition(status, cutoff)  (shared SQL)
  reporting/
    rules.py      pure: date-range validation, range -> UTC window, group totals
    schemas.py    AdminReservationItem, CapacityRow, RevenueOut, ... (own models)
    service.py    aggregate queries over an AsyncConnection
    router.py     one router, all routes `Depends(require_admin)`, `conn: DbConnection`
  reservations/service.py    list_reservations now calls db.reservation_filters
  main.py                    includes the reporting router
```

Service functions take an `AsyncConnection`, never commit, never open a
connection; `now` is passed in (hold expiry). `reporting` does not reuse
`reservations.schemas`; its items have a different shape (they contain the user).

### 2.3 Endpoints

All: admin only (anonymous 401, non-admin 403).

**`GET /admin/reservations`** — query: `movie_id`, `showtime_id`, `date_from`,
`date_to`, `status` (`held|confirmed|cancelled|expired`, effective), `limit`,
`offset`. Response: `Page[AdminReservationItem]`:
`id, status (effective), created_at, expires_at (held only), confirmed_at,
cancelled_at, seats [{seat_id,row_label,seat_number}], total_cents,
user {id,email}, showtime {id, starts_at, price_cents, movie {id,title},
screen {id,name}}`. Order `created_at DESC, id DESC`.

**`GET /admin/reports/capacity`** — query: `showtime_id`, `movie_id`,
`date_from`, `date_to`, `limit`, `offset`. Response: `Page[CapacityRow]`:
`showtime {id, starts_at, price_cents, movie {id,title}, screen {id,name}},
total_seats, sold_seats, held_seats, available_seats`. Order `starts_at, id`.
Showtimes with no reservations are included (all seats available).

**`GET /admin/reports/revenue`** — query: `date_from`, `date_to`, `movie_id`,
`group_by` (`movie|day`, default `movie`). Response:
`{date_from, date_to, group_by, total_cents, tickets_sold, rows: [...]}`.
Rows for `movie`: `{movie_id, title, tickets_sold, revenue_cents}`, ordered by
`revenue_cents DESC, movie_id`. Rows for `day`: `{date, tickets_sold,
revenue_cents}` ordered by `date`. `total_cents` and `tickets_sold` are the sums
of the rows, computed from the same single query result.

Unknown `movie_id` / `showtime_id` filters return empty results (200), as in
SPEC-3. `date_from > date_to` → 422 `INVALID_DATE_RANGE`. Invalid values
(`status`, `group_by`, dates, `limit`, `offset`) → 422 `VALIDATION_ERROR`.
Pagination as in SPEC-3 (`limit` default 20, 1-100; `offset` >= 0).

### 2.4 Error codes

| Code | Status |
|---|---|
| `INVALID_DATE_RANGE` (new): `date_from > date_to`, or `group_by=day` without both dates, or longer than 366 days | 422 |
| `VALIDATION_ERROR`, `UNAUTHENTICATED`, `FORBIDDEN` | 422, 401, 403 |

### 2.5 Query design

Date window: `rules.range_bounds_utc(date_from, date_to, CINEMA_TIMEZONE)` →
`[start, end)` using the SPEC-3 day-bounds logic from `showtimes.rules`
(`start` = local midnight of `date_from`, `end` = local midnight of the day
after `date_to`, both in UTC); a missing bound means unbounded on that side.
All filters on `showtimes.starts_at` use this window.

**Admin list.** One JOIN query (reservations, users, showtimes, movies,
screens) with the filters, `effective_status_condition` for `status`, ordered
and paged; one extra query for the page's seats (no N+1); one count query with
the same filters.

**Capacity.** One query over `showtimes JOIN movies JOIN screens` with three
correlated/grouped aggregates: `total_seats` = count of `seats` of the screen;
`sold_seats` = count of `seat_reservations` with status `confirmed`;
`held_seats` = count of `seat_reservations` with status `held` whose
reservation has `created_at > cutoff` (join to `reservations`). Page ordered
by `starts_at, id`; separate count of matching showtimes. `available_seats =
total - sold - held` is computed in `rules` (never negative; the unique index
makes `sold + held <= total` an invariant).

**Revenue.** One aggregate query:
`SELECT <group key>, COUNT(*) AS tickets, SUM(showtimes.price_cents) AS revenue
FROM seat_reservations JOIN showtimes (and movies for the title) WHERE
seat_reservations.status = 'confirmed' AND <window> [AND movie_id]
GROUP BY <key>`. Group key: `movies.id` or the showtime's local date
(`timezone(CINEMA_TIMEZONE, starts_at)::date`). `SUM` is converted to a Python
`int`. Totals are summed from the returned rows in Python, so total and rows
can never disagree. Because one statement is one snapshot, the response is
internally consistent even while confirmations happen concurrently.

**Read-only.** Reports never update rows, including lazy expiry; they only
*interpret* old holds through `cutoff` (SPEC-4 rule).

### 2.6 Authentication vs authorization

Every route depends on `require_admin` (authentication first: 401, then 403).
There are no ownership rules; admins see all.

---

## 3. Edge Cases & Constraints

- A day with no showtimes, or a range with no confirmed seats: 200 with zero
  totals and empty `rows` (revenue) or an empty page (lists).
- A showtime at local 00:00 of `date_from` is included; local 23:59 of
  `date_to` is included; local 00:00 of the day after `date_to` is excluded.
- A showtime at 23:30 Tbilisi belongs to its Tbilisi date, not the UTC date.
- A `held` reservation older than the hold window is `expired` in the admin
  list and in the `status` filter, and is not counted as held in capacity.
- Cancelling a confirmed reservation lowers `sold_seats` and revenue by exactly
  its seats × price.
- Prices are integers; `SUM` of up to millions of rows of values up to
  100 000 000 fits a Python int. No floats appear in any response.
- Reports ignore the showtime's started/not-started state: a past showtime's
  confirmed seats are revenue, a future showtime's confirmed seats are revenue.
- Page and total of the lists are separate statements (READ COMMITTED), so
  under heavy concurrent writes `total` may differ from the page by in-flight
  changes; accepted and documented. Revenue is a single statement and exact.

---

## 4. Configuration and dependency changes

None: no new settings, dependencies, migrations or indexes. `main.py` includes
the new router. SPEC-5's `list_reservations` is edited to use the shared SQL
filter (behavior unchanged).

---

## 5. Testing Requirements

Unit (no DB, no HTTP), `tests/unit/`:

1. `test_date_range_validation` — `from > to` rejected; `from == to` accepted;
   `group_by=day` without one of the dates rejected; 366 days accepted, 367
   rejected
2. `test_report_params_validation` — `group_by` and `status` accepted/rejected
   values; `limit` 0/1/100/101; `offset` -1/0
3. `test_range_bounds_utc_tbilisi_and_dst_zone` — Tbilisi window for one day
   and for a multi-day range; a DST-zone range; missing bounds are unbounded
4. `test_revenue_totals_equal_sum_of_rows_and_available_is_never_negative`

Integration (real Postgres via `TEST_DATABASE_URL`, httpx `ASGITransport`,
rolled-back connection per test unless stated),
`tests/integration/test_reporting_api.py`:

Access:
5. `test_all_report_routes_return_401_anonymous_and_403_for_user`

Admin reservation list:
6. `test_admin_reservations_lists_all_users_newest_first_with_user_and_showtime`
7. `test_admin_reservations_filter_by_movie_showtime_and_status`
8. `test_admin_reservations_status_filter_uses_effective_status`
9. `test_admin_reservations_date_range_uses_showtime_date_in_cinema_timezone`
10. `test_admin_reservations_pagination_total_and_bounds_return_422`
11. `test_admin_reservations_invalid_date_range_returns_422`

Capacity:
12. `test_capacity_counts_total_sold_held_and_available_per_showtime`
13. `test_capacity_ignores_cancelled_and_expired_and_stale_holds`
14. `test_capacity_filters_order_and_pagination`
15. `test_capacity_showtime_without_reservations_is_fully_available`

Revenue:
16. `test_revenue_counts_only_confirmed_seats_times_price`
17. `test_revenue_excludes_held_cancelled_and_expired`
18. `test_revenue_by_movie_ordered_by_revenue_desc_and_total_matches_rows`
19. `test_revenue_by_day_uses_cinema_timezone`
20. `test_revenue_date_range_boundaries_are_inclusive_and_exclusive_as_specified`
21. `test_revenue_group_by_day_requires_range_and_max_366_days`
22. `test_revenue_empty_range_returns_zero_totals_and_no_rows`
23. `test_revenue_reconciles_with_capacity_sold_seats_times_price`
24. `test_revenue_values_are_integers_never_floats`
25. `test_cancel_removes_revenue_and_frees_capacity` — confirm then cancel via
    the API; revenue and `sold_seats` return to the earlier values

Safety:
26. `test_reports_are_read_only` — row counts of reservations and seat rows
    unchanged after calling every report, including with stale holds present
27. `test_spec5_list_unchanged_after_shared_filter_refactor` — a short
    behavior check of the SPEC-5 effective-status filter (the full SPEC-5
    suite must also stay green)
28. `test_error_bodies_use_standard_shape_for_new_code`

Concurrency (committed connections via `committing_client`, own data, full
cleanup in `finally`):

29. `test_revenue_stays_consistent_while_confirmations_happen` — 20
    holds+confirms by 20 different users run concurrently with 20 revenue and
    capacity reads; in every read `total_cents == sum(rows)`, `revenue ==
    tickets_sold × price` (one price in the fixture) and `sold + held <= total`;
    after all tasks finish, revenue == confirmed seats × price exactly

### 5.1 Concurrency verification gate

Test 29 must pass **five consecutive runs** (as in SPEC-4 5.1 and SPEC-5 5.1);
the Implementation Status records the five results. Also run the full SPEC-5
suite after the filter refactor and record it.

---

## 6. Acceptance Criteria (EARS)

  FR-1  IF a caller is unauthenticated, THE SYSTEM SHALL answer 401 on every
        report route; IF the caller is authenticated but not an admin, THE
        SYSTEM SHALL answer 403.
  FR-2  WHEN an admin lists reservations, THE SYSTEM SHALL return all users'
        reservations newest first, each with its user (id and email), seats,
        showtime, movie, screen, effective status and `total_cents`, filtered
        by `movie_id`, `showtime_id`, showtime date range and effective status
        when given, and paginated.
  FR-3  THE SYSTEM SHALL interpret `date_from` / `date_to` as inclusive calendar
        days of the showtime in `CINEMA_TIMEZONE`; IF `date_from > date_to`, THE
        SYSTEM SHALL answer 422 `INVALID_DATE_RANGE`.
  FR-4  WHEN an admin requests capacity, THE SYSTEM SHALL return for each
        matching showtime `total_seats`, `sold_seats` (confirmed), `held_seats`
        (unexpired holds) and `available_seats = total - sold - held`, counting
        neither cancelled nor expired reservations nor stale holds.
  FR-5  THE SYSTEM SHALL compute revenue as the sum of the showtime's
        `price_cents` over confirmed seat rows only, in integer cents,
        excluding held, cancelled and expired seats.
  FR-6  WHEN an admin requests revenue, THE SYSTEM SHALL return `total_cents`,
        `tickets_sold` and rows grouped by movie (default) or by day, with the
        totals equal to the sum of the rows; IF `group_by=day` has no complete
        range or a range longer than 366 days, THE SYSTEM SHALL answer 422
        `INVALID_DATE_RANGE`.
  FR-7  THE SYSTEM SHALL derive revenue and capacity from the same facts so that
        for any filter, revenue equals the sum over showtimes of
        `sold_seats × price_cents`.
  FR-8  THE SYSTEM SHALL NOT write to the database in any report route.
  FR-9  THE SYSTEM SHALL paginate the two list endpoints with `limit` (1-100,
        default 20) and `offset` (>= 0), reject out-of-range values with 422,
        and report `total`.
  FR-10 THE SYSTEM SHALL define the "effective status" SQL condition in one
        place shared by the SPEC-5 list and the admin list, and SHALL NOT change
        the behavior of any SPEC-2..SPEC-5 endpoint.
  FR-11 THE SYSTEM SHALL expose about a user only `id` and `email`, and only to
        admins, and never any password hash.
  FR-12 THE SYSTEM SHALL return every error in the standard
        `{"error": {"code", "message"}}` shape.

### 6.1 Requirements Mapping

| Requirement | Design decision / section | Tests |
|---|---|---|
| FR-1 | 2.6, D4 | 5 |
| FR-2 | 2.3, D5, 2.5 admin list | 6, 7, 8, 9, 10 |
| FR-3 | D2, 2.5 date window | 1, 3, 9, 11, 20 |
| FR-4 | D3, 2.5 capacity | 12, 13, 14, 15 |
| FR-5 | D1, 2.5 revenue | 16, 17, 24 |
| FR-6 | D6, 2.3 | 4, 18, 19, 21, 22 |
| FR-7 | 1, 2.5 | 23, 25, 29 |
| FR-8 | 2.5 "Read-only" | 26 |
| FR-9 | 2.3 | 2, 10, 14 |
| FR-10 | D7, 2.2 | 8, 27 |
| FR-11 | D5 | 6 |
| FR-12 | 2.4 | 28 |

### Equivalence Partitioning + Boundary Value Analysis

Date range (FR-3, FR-6):
```
from > to              invalid (422)
from == to             valid, one day
from < to              valid
group_by=day, range    1 .. 366 days valid; boundary 366 / 367
group_by=day, a missing bound: invalid
```
Showtime vs window (FR-3, tests 20): local `from 00:00` included, `from 00:00 - 1 s`
excluded, `to 23:59:59` included, `to + 1 day 00:00` excluded.
Seat status vs revenue (FR-5): `confirmed` counted; `held` fresh, `held` stale,
`cancelled`, `expired` not counted.
Hold age vs capacity (FR-4), `HOLD_MINUTES` = 10: age 9:59 counted as held; age
10:00 not counted.
`limit`: 0 / 1 and 100 / 101. `offset`: -1 / 0.

---

## 7. Logging Requirements

- Each report request: `INFO` with event name, admin user id, and the filter
  names used (not the email addresses returned).
- Never log the returned data, emails, tokens or request bodies.
- Unexpected exceptions: `ERROR` with traceback (global handler).

---

## 8. Reuse/Dependency Check

No new dependency. Reused: `require_admin`, `DbConnection`, `ApiError`,
`Page` / `PageParams`, `showtimes.rules` day-bounds logic and
`reservations.rules` (`hold_cutoff`, `effective_status`) as pure functions,
the status vocabulary in `core/enums.py`, and the test builders and committing
client from earlier SPECs.

---

## 9. Non-Goals / Revisit

Out of scope in v1: CSV/Excel/PDF export, charts (frontend), caching or
materialized views, per-user reports and a `user_id` filter, revenue by screen
or by genre, comparison with previous periods, scheduled reports,
tax/VAT, currencies other than the integer-cents unit, payment-date based
revenue (there is no payment), real-time push updates.

Revisit if: a report becomes slow with real volumes (add an index or a summary
table, which needs a SPEC-1 amendment); real payments arrive (revenue would then
follow payment records, not seat rows); several cinemas or currencies are added.

---

## 10. DRY Check

- Effective-status SQL condition: only `db/reservation_filters.py` (D7).
- Effective-status Python logic and the expiry cutoff: only
  `reservations/rules.py`.
- Day-bounds logic: only `showtimes/rules.py`; `reporting/rules.py` builds
  range bounds on top of it and does not copy it.
- Page envelope and params: the shared `Page` / `PageParams`.
- Status vocabulary: the single definition in `core/enums.py`.
- Cinema timezone: only from settings.

## 11. Single Responsibility Check

- `reporting/rules.py`: pure date-range and arithmetic logic.
- `reporting/service.py`: queries (no HTTP types).
- `reporting/schemas.py`: validation and response shape.
- `reporting/router.py`: parsing, `require_admin`, status codes only.
- `db/reservation_filters.py`: building one SQL condition, nothing else.

## 11.1 Layer & Dependency Check

Allowed imports: `reporting` -> `core`, `db`, `auth.dependencies`, and the
**pure** modules `reservations.rules` and `showtimes.rules`. `reporting` does
not import any other `reservations` / `showtimes` / `movies` module. `reservations`
-> `core`, `db`, `auth.dependencies` (it now also imports
`db.reservation_filters`). `db` -> `core`. Nothing imports `reporting`.

---

## Amendment Log

(none yet)

---

## Implementation Status

NOT STARTED
