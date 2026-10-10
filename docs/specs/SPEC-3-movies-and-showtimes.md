# SPEC-3: Movie & Showtime Management

Feature:      Movie + genre catalog, screens with generated seats, showtimes,
              public browsing by date
Priority:     P2 (Catalog & Scheduling)
Status:       See SPEC-3.status — this line is for human reference only
Dependencies: SPEC-1 (tables), SPEC-2 (`require_admin`, `DbConnection`,
              `ApiError`, error shape)
Related Docs: APP-OVERVIEW.md, steering/tech-stack.md, steering/conventions.md,
              steering/principles.md, steering/product.md,
              specs/SPEC-1-database-schema.md, specs/SPEC-2-auth-and-roles.md

---

## 1. Overview

First SPEC that builds on the API skeleton from SPEC-2. It delivers:

1. Genres: list (public), create and delete (admin).
2. Movies: public list and detail; admin create, partial update, delete.
3. Screens: admin creates a screen from structured input (`rows` ×
   `seats_per_row`); the seats are generated in the same transaction. Admin
   lists screens. No editor, no update, no delete (v1 non-goal).
4. Showtimes: admin creates and deletes; public browsing by date.
5. The scheduling rules SPEC-1 explicitly delegated to this SPEC:
   `starts_at` must be in the future, and two showtimes on one screen must
   not overlap.

No new table and no schema change: **SPEC-1 stays frozen** (see Decision D1
in 2.1). One new runtime dependency (`tzdata`) and two new settings; both
need sign-off with approval (Section 8).

Technology: FastAPI, Pydantic v2, SQLAlchemy Core (async), `zoneinfo`.

---

## 2. Design

### 2.1 Decisions (approved by George 2026-10-10: D1 = option A with 180 minutes, D2-D6 as written)

**D1 — Overlap rule without a movie runtime (recommended: fixed slot).**
`movies` has no runtime column. Two options:

- **A (recommended): fixed slot.** Two showtimes on the same screen must start
  at least `SHOWTIME_SLOT_MINUTES` apart (default 180, allowed 30–600). No
  schema change; SPEC-1 stays frozen. Less realistic (a 90-minute film
  blocks 180 minutes) but simple and safe.
- **B: add `movies.duration_minutes`.** Overlap = runtime + cleanup buffer.
  More realistic, but it is a schema change after approval: a SPEC-1
  amendment, a new Alembic migration, and a change to the seed. Per
  `product.md` this requires explicit sign-off.

This SPEC is written for **A**. If you pick B, tell me before approving; I
will rewrite 2.4, 2.5, the tests and add the SPEC-1 amendment first.

**D2 — Date browsing timezone.** `starts_at` is stored in UTC. A "date" means
a calendar day in `CINEMA_TIMEZONE` (default `Asia/Tbilisi`). This needs the
IANA database on Windows, hence the `tzdata` dependency. (Alternative: a
fixed UTC offset setting, no dependency, but wrong if the cinema ever moves
to a DST zone.)

**D3 — Deleting a movie** is refused with 409 if it has *any* showtime, past
or future (SPEC-1 uses `ON DELETE RESTRICT`; the module pre-checks to return
a clean error). `product.md` mentions only future showtimes; the stricter rule
keeps history of past showtimes intact.

**D4 — Showtime deletion** is allowed only if it has not started and has no
reservations. There is no showtime update in v1 (changing time or price under
existing reservations is dangerous); to change one, delete and recreate.

**D5 — Admin screen creation** (`POST /admin/screens`) is included so seating
is defined through structured input, as `product.md` says, not by hand-edited
SQL. Seats are all `standard`; accessible seats are not creatable through the
API in v1 (the column exists for later).

**D6 — Referenced ids in a body** (`genre_ids`, `movie_id`, `screen_id`) that
do not exist return 404 (`GENRE_NOT_FOUND`, `MOVIE_NOT_FOUND`,
`SCREEN_NOT_FOUND`), not 422.

### 2.2 Module layout

```
backend/src/
  core/
    config.py        + CINEMA_TIMEZONE, SHOWTIME_SLOT_MINUTES (validated)
  movies/            genres and movies
    schemas.py       GenreIn/Out, MovieCreate/MovieUpdate/MovieOut, Page
    service.py       business rules over an AsyncConnection
    router.py        public_router + admin_router
  showtimes/         screens and showtimes
    schemas.py
    rules.py         pure functions: slot overlap, day bounds (no DB)
    service.py
    router.py        public_router + admin_router
  main.py            includes the four routers
```

Routers live in the modules; `main.py` only includes them. All handlers use
`conn: DbConnection` and `Depends(require_admin)` for admin routes. Service
functions take an `AsyncConnection`, never commit, never open a connection.

### 2.3 Endpoints

Public (no token):

| Method | Path | Result |
|---|---|---|
| GET | `/genres` | 200 list of genres, ordered by name |
| GET | `/movies?genre_id=&limit=&offset=` | 200 page of movies with genres |
| GET | `/movies/{movie_id}` | 200 `MovieOut` / 404 `MOVIE_NOT_FOUND` |
| GET | `/showtimes?date=&movie_id=&limit=&offset=` | 200 page of showtimes |
| GET | `/showtimes/{showtime_id}` | 200 `ShowtimeOut` / 404 `SHOWTIME_NOT_FOUND` |

Admin (`require_admin`; anonymous 401, non-admin 403):

| Method | Path | Result |
|---|---|---|
| POST | `/admin/genres` | 201 `GenreOut` / 409 `GENRE_ALREADY_EXISTS` |
| DELETE | `/admin/genres/{genre_id}` | 204 / 404 `GENRE_NOT_FOUND` / 409 `GENRE_IN_USE` |
| POST | `/admin/movies` | 201 `MovieOut` |
| PATCH | `/admin/movies/{movie_id}` | 200 `MovieOut` / 404 |
| DELETE | `/admin/movies/{movie_id}` | 204 / 404 / 409 `MOVIE_HAS_SHOWTIMES` |
| GET | `/admin/screens` | 200 list of screens (id, name, rows, seats_per_row) |
| POST | `/admin/screens` | 201 `ScreenOut` / 409 `SCREEN_NAME_TAKEN` |
| POST | `/admin/showtimes` | 201 `ShowtimeOut` |
| DELETE | `/admin/showtimes/{showtime_id}` | 204 / 404 / 409 |

Page envelope (movies and showtimes lists):
`{"items": [...], "total": N, "limit": L, "offset": O}`. `limit` default 20,
1–100; `offset` default 0, ≥ 0. Out-of-range values → 422 `VALIDATION_ERROR`
(not silently clamped).

`MovieOut`: `id, title, description, poster_url (nullable), genres:
[{id, name}] sorted by name, created_at, updated_at`.

`ShowtimeOut`: `id, starts_at (UTC, ISO 8601 with Z), price_cents,
movie: {id, title, poster_url}, screen: {id, name}`. (Seat availability is
SPEC-4, not here.)

`ScreenOut`: `id, name, rows, seats_per_row, seat_count`.

### 2.4 Input rules (enforced in Pydantic models and the `movies` / `showtimes` modules)

All request models use `extra="forbid"`.

- Genre `name`: stripped, 1–50 chars after stripping.
- Movie `title`: stripped, 1–200 chars. `description`: 0–5000 chars (default
  `""`). `poster_url`: optional, absolute `http`/`https` URL, ≤ 2048 chars; a
  poster is a URL only (no upload). `genre_ids`: list of distinct ints, 0–10
  items (duplicates → 422).
- Movie update (`PATCH`): same field rules, every field optional; at least one
  field must be present (empty body → 422). `poster_url: null` clears the
  poster. `genre_ids` present means "replace the whole set"; absent means
  "leave unchanged".
- Screen `name`: stripped, 1–50 chars. `rows`: 1–26 (row labels `A`–`Z`).
  `seats_per_row`: 1–40.
- Showtime `starts_at`: must carry a timezone (a naive datetime → 422); it is
  converted to UTC. Must be strictly in the future at creation
  (`SHOWTIME_IN_PAST`, 422). `price_cents`: integer, 1 to 100 000 000 (the DB
  only requires > 0; the upper bound guards against typos and overflow).
  `price_cents` as a float or string → 422 (strict int).
- `GET /showtimes?date=`: `YYYY-MM-DD`; anything else → 422.

### 2.5 Business rules

**Movie create/update.** Verify every `genre_id` exists (one query,
`WHERE id IN (...)`), else 404 `GENRE_NOT_FOUND`. Update sets
`updated_at = now()` in the module (SPEC-1: there is no DB trigger). The
genre set is replaced in the same transaction (delete old links, insert new).

**Movie delete.** Pre-check `EXISTS (showtime for movie)` → 409
`MOVIE_HAS_SHOWTIMES`. `movie_genres` rows are removed by the existing
`ON DELETE CASCADE`. The RESTRICT FK stays as the safety net: an
`IntegrityError` from a showtime inserted concurrently is also mapped to the
same 409.

**Genre create.** Case-insensitive duplicate check
(`lower(name) = lower(:name)`) → 409 `GENRE_ALREADY_EXISTS`; the insert runs
in `begin_nested()` and an `IntegrityError` from the UNIQUE constraint is
mapped to the same 409 (same pattern as `signup`). Note: two simultaneous
requests that differ only by case could both pass the DB constraint (it is
case-sensitive); this is accepted as cosmetic and documented.

**Genre delete.** `EXISTS (movie_genres for genre)` → 409 `GENRE_IN_USE`.

**Screen create.** Insert the screen (savepoint, `IntegrityError` → 409
`SCREEN_NAME_TAKEN`), then insert all `rows × seats_per_row` seats in one
bulk statement: `row_label` = `A`..`Z`, `seat_number` = 1..N,
`seat_type` = `standard`. All in the request transaction: either the screen
and all seats exist or nothing does.

**Showtime create.**
1. Load movie and screen (404 if missing).
2. `starts_at` must be > `now` (UTC) else 422 `SHOWTIME_IN_PAST`.
3. Lock the screen row: `SELECT ... FROM screens WHERE id = :id FOR UPDATE`.
   This serializes concurrent showtime creation on the same screen, so the
   overlap check below cannot race (there is no DB overlap constraint).
4. Overlap check: conflict if any showtime on that screen has `starts_at`
   strictly between `S - slot` and `S + slot` (exclusive both ends; so a
   showtime exactly `slot` minutes apart is allowed). Conflict → 409
   `SHOWTIME_OVERLAP`. The window arithmetic lives in the pure function
   `rules.windows_overlap` / `rules.conflict_window`.
5. Insert and return `ShowtimeOut`.

**Showtime delete.** 404 if missing; `starts_at <= now` → 409
`SHOWTIME_STARTED`; any `reservations` row for the showtime → 409
`SHOWTIME_HAS_RESERVATIONS` (any status: history is kept). The RESTRICT FK is
the safety net here too.

**Browse showtimes.** `date` → window `[local 00:00, next local 00:00)` in
`CINEMA_TIMEZONE`, converted to UTC (`rules.day_bounds_utc`; correct on
23/25-hour DST days). Without `date`: all showtimes with `starts_at >= now`.
Optional `movie_id` filter (no 404 for an unknown movie: empty list). Order
`starts_at, id`. Uses `idx_showtimes_date` / `idx_showtimes_movie_date`.
Movie and screen data come from a single JOIN, not one query per row.

**Browse movies.** Optional `genre_id` filter (unknown genre: empty list).
Order `lower(title), id`. Genres for the page are loaded with a single
additional query (`WHERE movie_id IN (...)`), no N+1.

### 2.6 Authentication vs authorization

Unchanged from SPEC-2: admin routes depend on `require_admin` (authentication
first, so anonymous = 401, non-admin = 403). Reads are public. No ownership
rules exist in this SPEC.

### 2.7 Error shape

All errors use `ApiError` and the standard
`{"error": {"code", "message"}}` body. New codes:

| Code | Status |
|---|---|
| `GENRE_NOT_FOUND`, `MOVIE_NOT_FOUND`, `SCREEN_NOT_FOUND`, `SHOWTIME_NOT_FOUND` | 404 |
| `GENRE_ALREADY_EXISTS`, `SCREEN_NAME_TAKEN` | 409 |
| `GENRE_IN_USE`, `MOVIE_HAS_SHOWTIMES` | 409 |
| `SHOWTIME_OVERLAP`, `SHOWTIME_STARTED`, `SHOWTIME_HAS_RESERVATIONS` | 409 |
| `SHOWTIME_IN_PAST` | 422 |

Constants live in one place per module (no bare code strings scattered).

---

## 3. Edge Cases & Constraints

- Movie with zero genres is valid. Description may be empty.
- Updating a movie to the same values still bumps `updated_at`.
- Deleting a movie after its only showtime was deleted works.
- Two admins creating showtimes on the same screen at the same instant:
  exactly one wins, the other gets 409 (screen row lock, test 33).
- Showtime exactly `slot` minutes after another on the same screen: allowed.
  One minute less: 409. Different screens: never conflict.
- `starts_at` given with a non-UTC offset (`+04:00`) is accepted and stored as
  the equivalent UTC instant; responses are always UTC.
- A date near midnight Tbilisi (e.g. 23:30 local) appears under the local
  date, not the UTC date (test 40).
- Changing `SHOWTIME_SLOT_MINUTES` later is not retroactive: existing
  showtimes are never re-validated.
- Seats of a screen with showtimes are never modified (no seat editing exists).
- `GET /showtimes` for a date with no showtimes returns 200 with an empty page.

---

## 4. Configuration and dependency changes

`core/config.py` gets two settings (with defaults, documented in
`.env.example`):

- `CINEMA_TIMEZONE` (default `Asia/Tbilisi`): must be a valid IANA name,
  otherwise the app refuses to start (same style as the JWT secret rule).
- `SHOWTIME_SLOT_MINUTES` (default 180): integer 30–600, otherwise the app
  refuses to start.

`pyproject.toml` gets `tzdata` (runtime). No change to the seed script, to
migrations or to SPEC-1.

---

## 5. Testing Requirements

Unit (no DB, no HTTP), `tests/unit/`:

1. `test_windows_overlap_boundaries` — gap slot-1 min conflicts; exactly slot
   min does not; 0 gap conflicts; symmetric (before and after)
2. `test_day_bounds_utc_tbilisi` — `2026-10-12` → `2026-10-11T20:00Z` to
   `2026-10-12T20:00Z`
3. `test_day_bounds_utc_handles_dst_day` — a 23-hour and a 25-hour day in a
   DST zone (e.g. `Europe/Berlin`) have the right length
4. `test_settings_reject_invalid_timezone_and_slot` — bad IANA name; slot 29,
   30, 600, 601
5. `test_movie_schema_boundaries` — title 0/1/200/201 chars, whitespace-only
   title, description 5000/5001, poster URL scheme/length, genre_ids
   duplicates and > 10
6. `test_showtime_schema_rejects_naive_datetime_float_price_and_extra_fields`
7. `test_screen_schema_boundaries` — rows 0/1/26/27, seats_per_row 0/1/40/41

Integration (real Postgres via `TEST_DATABASE_URL`, httpx `ASGITransport`,
rolled-back connection per test), `tests/integration/test_movies_api.py`,
`test_showtimes_api.py`:

Auth and shape:
8. `test_public_reads_need_no_token`
9. `test_admin_writes_return_401_anonymous_and_403_for_user` — every admin route
10. `test_error_bodies_use_standard_shape_for_new_codes`

Genres:
11. `test_create_genre_trims_and_lists_sorted`
12. `test_create_genre_duplicate_case_insensitive_returns_409`
13. `test_delete_genre_in_use_returns_409_and_unused_returns_204`

Movies:
14. `test_create_movie_with_genres_returns_201`
15. `test_create_movie_unknown_genre_returns_404_and_creates_nothing`
16. `test_create_movie_validation_failures_return_422`
17. `test_patch_movie_updates_only_given_fields_and_bumps_updated_at`
18. `test_patch_movie_replaces_genre_set_and_null_clears_poster`
19. `test_patch_movie_empty_body_returns_422_and_unknown_id_404`
20. `test_delete_movie_without_showtimes_returns_204_and_removes_genre_links`
21. `test_delete_movie_with_showtime_returns_409`
22. `test_list_movies_pagination_total_and_order`
23. `test_list_movies_genre_filter_and_limit_offset_bounds_422`
24. `test_get_movie_unknown_returns_404`

Screens:
25. `test_create_screen_generates_exactly_rows_times_seats`
26. `test_create_screen_labels_and_types` — rows A..; numbers from 1; all
    `standard`
27. `test_create_screen_duplicate_name_returns_409_and_leaves_no_orphan_seats`
28. `test_list_screens_returns_seeded_and_created`

Showtimes:
29. `test_create_showtime_returns_201_and_utc_response`
30. `test_create_showtime_offset_input_is_normalized_to_utc`
31. `test_create_showtime_in_past_returns_422` — service-level boundary:
    `now + 1 s` accepted, `now` and `now - 1 s` rejected (service called with
    an explicit `now`)
32. `test_create_showtime_unknown_movie_or_screen_returns_404`
33. `test_concurrent_overlapping_showtimes_exactly_one_succeeds` — two
    simultaneous requests on committed connections; cleans up in `finally`
34. `test_overlap_same_screen_409_exact_slot_gap_ok_other_screen_ok`
35. `test_price_boundaries` — 0, -1 rejected; 1 accepted; 100 000 000 accepted;
    100 000 001 rejected
36. `test_delete_showtime_future_without_reservations_returns_204`
37. `test_delete_showtime_started_returns_409`
38. `test_delete_showtime_with_reservation_returns_409` — a reservation row is
    inserted directly with SQL
39. `test_list_showtimes_by_date_filters_orders_and_embeds_movie_and_screen`
40. `test_list_showtimes_date_uses_cinema_timezone_not_utc`
41. `test_list_showtimes_without_date_returns_only_future`
42. `test_list_showtimes_movie_filter_pagination_and_bad_date_422`

---

## 6. Acceptance Criteria (EARS)

  FR-1  THE SYSTEM SHALL serve genre, movie and showtime reads to
        unauthenticated callers.
  FR-2  IF a caller is unauthenticated, THE SYSTEM SHALL answer 401 on every
        `/admin/...` route; IF the caller is authenticated but not an admin,
        THE SYSTEM SHALL answer 403.
  FR-3  WHEN an admin submits a valid movie, THE SYSTEM SHALL create it with
        its genre links and return 201 `MovieOut`.
  FR-4  IF a movie or genre request violates the field rules (lengths, URL
        scheme, duplicate or too many `genre_ids`, extra fields, empty
        update), THE SYSTEM SHALL answer 422 `VALIDATION_ERROR` and change
        nothing.
  FR-5  IF a request names a genre, movie, screen or showtime that does not
        exist, THE SYSTEM SHALL answer 404 with the matching `*_NOT_FOUND`
        code and change nothing.
  FR-6  WHEN an admin updates a movie, THE SYSTEM SHALL change only the
        supplied fields, replace the genre set only if `genre_ids` is
        supplied, and set `updated_at`.
  FR-7  IF a movie has any showtime, THE SYSTEM SHALL refuse to delete it
        with 409 `MOVIE_HAS_SHOWTIMES`; otherwise it SHALL delete it and its
        genre links.
  FR-8  IF a genre name matches an existing one ignoring case, THE SYSTEM
        SHALL answer 409 `GENRE_ALREADY_EXISTS`; IF a genre is attached to
        any movie, THE SYSTEM SHALL refuse to delete it with 409
        `GENRE_IN_USE`.
  FR-9  WHEN an admin creates a screen, THE SYSTEM SHALL create it together
        with exactly `rows × seats_per_row` `standard` seats, labelled by row
        letter and seat number, atomically; IF the name is taken, THE SYSTEM
        SHALL answer 409 `SCREEN_NAME_TAKEN` and create no seats.
  FR-10 IF `starts_at` has no timezone, THE SYSTEM SHALL answer 422; IF it is
        not strictly in the future, THE SYSTEM SHALL answer 422
        `SHOWTIME_IN_PAST`; IF `price_cents` is not an integer from 1 to
        100 000 000, THE SYSTEM SHALL answer 422.
  FR-11 IF a new showtime starts less than `SHOWTIME_SLOT_MINUTES` before or
        after another showtime on the same screen, including when two such
        requests arrive concurrently, THE SYSTEM SHALL accept at most one and
        answer the others 409 `SHOWTIME_OVERLAP`.
  FR-12 IF a showtime has started, THE SYSTEM SHALL refuse to delete it with
        409 `SHOWTIME_STARTED`; IF it has any reservation, THE SYSTEM SHALL
        refuse with 409 `SHOWTIME_HAS_RESERVATIONS`.
  FR-13 WHEN `date` is given, THE SYSTEM SHALL return exactly the showtimes
        whose start falls on that calendar day in `CINEMA_TIMEZONE`, ordered
        by start time; WHEN it is omitted, only showtimes that have not yet
        started.
  FR-14 THE SYSTEM SHALL paginate movie and showtime lists with `limit`
        (1–100, default 20) and `offset` (≥ 0), reject out-of-range values
        with 422, and report `total`.
  FR-15 THE SYSTEM SHALL store all instants as UTC, return `starts_at` in
        UTC, and all prices as integer cents.
  FR-16 THE SYSTEM SHALL return every error from these endpoints in the
        standard `{"error": {"code", "message"}}` shape.

### 6.1 Requirements Mapping

| Requirement | Design decision / section | Tests |
|---|---|---|
| FR-1 | 2.3 public routes | 8 |
| FR-2 | 2.6 `require_admin` | 9 |
| FR-3 | 2.5 movie create | 14 |
| FR-4 | 2.4 input rules, `extra="forbid"` | 5, 6, 7, 16, 19 |
| FR-5 | D6, 2.5 | 15, 19, 24, 32 |
| FR-6 | 2.5 movie update | 17, 18 |
| FR-7 | D3, 2.5 movie delete | 20, 21 |
| FR-8 | 2.5 genre create/delete | 12, 13 |
| FR-9 | D5, 2.5 screen create | 25, 26, 27 |
| FR-10 | 2.4, 2.5 showtime create | 6, 29, 30, 31, 35 |
| FR-11 | D1, 2.5 step 3-4 (screen row lock) | 1, 33, 34 |
| FR-12 | D4, 2.5 showtime delete | 36, 37, 38 |
| FR-13 | D2, 2.5 browse | 2, 3, 39, 40, 41 |
| FR-14 | 2.3 page envelope | 22, 23, 42 |
| FR-15 | 2.3, SPEC-1 conventions | 29, 30 |
| FR-16 | 2.7 | 10 |

### Equivalence Partitioning + Boundary Value Analysis

Showtime price (FR-10):
```
Partition 1: <= 0                 (invalid)  boundary: 0 / 1
Partition 2: 1 .. 100 000 000     (valid)    boundary: 100 000 000 / 100 000 001
Partition 3: > 100 000 000        (invalid)
Non-integer (float, string)       (invalid)
```
Slot gap on one screen (FR-11), slot = 180 min:
```
gap < 180 (incl. 0)  conflict   boundary: 179 / 180
gap >= 180           allowed
```
Start time vs now (FR-10): `now - 1 s` rejected, `now` rejected, `now + 1 s` accepted.
Title length (FR-4): 0 and whitespace-only rejected; 1 and 200 accepted; 201 rejected.
Rows (FR-9): 0 / 1 and 26 / 27. Seats per row: 0 / 1 and 40 / 41.
`limit`: 0 / 1 and 100 / 101. `offset`: -1 / 0.

---

## 7. Logging Requirements

- Every admin write (create/update/delete of a genre, movie, screen,
  showtime) is logged at `INFO` with event name, acting admin id and the
  affected entity id (audit trail).
- Expected refusals (409 and 404 cases) are logged at `INFO` with event name.
- Unexpected exceptions: `ERROR` with traceback (global handler, SPEC-2).
- Never log request bodies wholesale.

---

## 8. Reuse/Dependency Check

New runtime dependency (needs sign-off with SPEC approval, `product.md`):

- `tzdata`: the IANA timezone database for `zoneinfo`; Windows has none.
  Pure data package from the Python core team's ecosystem (PSF), tiny, no
  transitive dependencies.

Reused unchanged: `require_admin`, `DbConnection`, `ApiError`/`error_body`,
the `begin_nested()` + `IntegrityError` pattern from `signup`, the test
fixtures (`rollback_connection`, `committing_client`, `make_admin`,
`auth_header`).

---

## 9. Non-Goals / Revisit

Out of scope in v1: movie runtime / rating / trailer / cast, poster upload,
showtime update, screen update or delete, seat editing, accessible-seat
creation, genre rename, full-text search, soft delete, bulk showtime
creation, seat availability on showtimes (SPEC-4).

Revisit if: real runtimes are needed for scheduling (take option B of D1);
the cinema moves to another timezone (change the setting, no code change).

---

## 10. DRY Check

- Slot/overlap arithmetic and day bounds: only in `showtimes/rules.py`.
- Page envelope and `limit`/`offset` validation: one shared definition (a
  common `Page` model and a `PageParams` dependency in `src/core`), used by
  movies and showtimes.
- Error codes: constants per module, not repeated strings.
- Cinema timezone and slot length: only from settings.
- Admin check: only `require_admin`.
- Seat generation: one function in `showtimes/service.py`; the seed script
  keeps its own explicit seed data (it creates two fixed screens through the
  same function if it is practical, otherwise it is noted as a known
  duplication in the Implementation Status).

## 11. Single Responsibility Check

- `rules.py`: pure date/overlap logic (no DB, no HTTP).
- `movies/service.py`, `showtimes/service.py`: business rules over a
  connection (no HTTP types).
- `schemas.py`: validation and response shape only.
- `router.py`: parsing, dependencies, response codes only.

## 11.1 Layer & Dependency Check

Allowed imports: `movies` -> `core`, `db`, `auth.dependencies`;
`showtimes` -> `core`, `db`, `auth.dependencies`; `db` -> `core`. `movies` and
`showtimes` do **not** import each other: both query `src.db.tables`
directly (e.g. the movie delete pre-check looks at the `showtimes` table, the
showtime create reads `movies`). The shared page model sits in `core`.

---

## Amendment Log

(none yet)

---

## Implementation Status

NOT STARTED
