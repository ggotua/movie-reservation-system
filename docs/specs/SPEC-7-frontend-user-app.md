# SPEC-7: Frontend — User App

Feature:      Customer-facing web app: sign up / log in, browse showtimes by date,
              seat map, hold and confirm seats, my reservations, cancel
Priority:     P6 (Frontend)
Status:       See SPEC-7.status — this line is for human reference only
Dependencies: SPEC-2 (auth), SPEC-3 (catalog), SPEC-4 (seats, hold, confirm),
              SPEC-5 (my reservations, cancel); small backend change: CORS
Related Docs: APP-OVERVIEW.md (5), steering/tech-stack.md, steering/conventions.md,
              steering/principles.md, steering/product.md, all backend SPECs

---

## 1. Overview

First frontend SPEC. A single-page app in `frontend/` that uses the existing
backend API and nothing else. It delivers the user workflows of
`APP-OVERVIEW.md` section 5:

1. Sign up, log in, log out; the session survives a page reload.
2. Browse showtimes for a chosen date; a movie page with its showtimes.
3. A seat map for a showtime (available / held / taken), refreshed
   automatically.
4. Select seats and **hold** them; a checkout page with a live countdown, then
   **confirm** (stubbed payment) or **release**.
5. "My reservations": list, filter, cancel a confirmed upcoming reservation.

It is also the end-to-end test of the backend: Section 5.3 is a manual
acceptance checklist against a real running server.

Out of scope here: the admin UI (SPEC-8: movies, showtimes, reports), i18n (UI
is English only), payment, notifications.

One small backend change is part of this SPEC: CORS (SPEC-2 deferred it "with
the frontend"). No schema change, no new backend dependency.

Technology (named in `tech-stack.md`): TypeScript (strict), React, Vite,
TanStack Query, Tailwind CSS, shadcn/ui. New (need sign-off, Section 8):
`react-router-dom`, and dev-only Vitest, React Testing Library, MSW, ESLint,
Prettier.

---

## 2. Design

### 2.1 Decisions (approved by George, 2026-10-30)

**D1 — New dependencies.** Runtime: `react-router-dom` (routing). Dev only:
`vitest`, `@testing-library/react`, `@testing-library/user-event`,
`@testing-library/jest-dom`, `msw`, `eslint` (+ `typescript-eslint`),
`prettier`. Package manager: **npm** (no extra install on Windows).
Nothing else (no form library, no schema library, no state library: plain
controlled forms, TanStack Query for server state, React state for the rest).

**D2 — CORS in the backend.** New setting `CORS_ORIGINS` (comma-separated
list, default `http://localhost:5173,http://127.0.0.1:5173`) and Starlette's
`CORSMiddleware` (part of the existing FastAPI install). Allowed methods:
`GET, POST, PATCH, DELETE, OPTIONS`; allowed headers: `Authorization,
Content-Type`; `allow_credentials=False` (a bearer header, no cookies).
A wildcard `*` is refused at startup. Error bodies keep the standard shape.

**D3 — Session storage.** The JWT is kept in `localStorage` behind one small
module. On `401` the app clears it and sends the user to login. Known trade-off:
a script injection could read it; mitigated by never rendering raw HTML (FR-11).
There are no refresh tokens (SPEC-2 non-goal), so the user logs in again after
the token expires.

**D4 — UI language: English only.** Messages for backend error codes live in
one file (`src/lib/errorMessages.ts`), which keeps a later Georgian translation
cheap.

**D5 — Currency and time zone are frontend settings.** The backend has no
currency field and returns UTC instants. The UI reads `VITE_CURRENCY` (default
`GEL`) and `VITE_CINEMA_TIMEZONE` (default `Asia/Tbilisi`, the same as the
backend's `CINEMA_TIMEZONE`). All times are shown in the **cinema** time zone,
never the browser's, so a showtime "date" always matches what the API filters by.

**D6 — Seat map refresh.** TanStack Query polling every 5 seconds while the tab
is visible (no WebSockets). The maximum seats per hold is a constant `8` that
mirrors the backend default; if the server answers `TOO_MANY_SEATS`, the UI shows
the message instead of failing.

**D7 — Admin users** see the same user app. Admin screens are SPEC-8.

**D8 — Folder and tooling.** `frontend/` at the repo root; Node version recorded
in `frontend/.nvmrc`; `package-lock.json` committed; scripts `dev`, `build`,
`lint`, `typecheck`, `test`. The dev server uses `VITE_API_BASE_URL` (default
`http://127.0.0.1:8000`).

**D9 — Look.** Clean, responsive, mobile-first, dark mode that follows the
system setting, shadcn/ui components with Tailwind. Accessibility is a
requirement, not polish (FR-12).

**D10 — CI.** The GitHub Actions workflow gets a `frontend` job: `npm ci`, lint,
typecheck, test, build (Section 5.2).

### 2.2 Folder layout

```
frontend/
  package.json  package-lock.json  .nvmrc  index.html  vite.config.ts
  tsconfig.json  eslint.config.js  .prettierrc  .env.example
  src/
    main.tsx  App.tsx            providers (QueryClient, Router, Auth)
    api/        client.ts  types.ts  auth.ts  catalog.ts  seats.ts  reservations.ts
    features/
      auth/          LoginPage  SignupPage  AuthContext  RequireAuth
      browse/        BrowsePage  MoviePage  ShowtimeCard
      seats/         SeatMapPage  SeatGrid  seatSelection.ts
      reservations/  CheckoutPage  MyReservationsPage  ReservationCard
    components/ui/   (shadcn/ui components)
    lib/        money.ts  time.ts  countdown.ts  errorMessages.ts  tokenStorage.ts
    test/       msw handlers, render helpers, fixtures
```

`api/types.ts` holds hand-written TypeScript types that mirror the backend
response models exactly (field names are snake_case as the API sends them).
Components never call `fetch` directly; they use the functions in `api/` through
TanStack Query hooks.

### 2.3 Routes

| Path | Page | Access |
|---|---|---|
| `/` | Browse: date picker + showtimes grouped by movie | public |
| `/movies/:movieId` | Movie details + its upcoming showtimes | public |
| `/showtimes/:showtimeId` | Seat map, selection, "Hold seats" | public view; hold needs login |
| `/checkout/:reservationId` | Countdown, Confirm, Release | logged in |
| `/reservations` | My reservations, filters, Cancel | logged in |
| `/login`, `/signup` | Forms | public |
| `*` | Not found | public |

Protected routes redirect anonymous users to `/login?next=<path>`; after login
the user returns to `next` (only same-origin relative paths are accepted).

### 2.4 API usage (all from existing endpoints)

| Screen | Calls |
|---|---|
| Session | `POST /auth/signup`, `POST /auth/login`, `GET /auth/me` |
| Browse | `GET /showtimes?date=YYYY-MM-DD&limit=100` (items embed movie and screen) |
| Movie page | `GET /movies/{id}`, `GET /showtimes?movie_id={id}` |
| Seat map | `GET /showtimes/{id}`, `GET /showtimes/{id}/seats` (polled) |
| Hold | `POST /reservations` |
| Checkout | `GET /reservations/{id}`, `POST .../confirm`, `POST .../release` |
| My reservations | `GET /reservations?status=&upcoming=&limit=&offset=`, `POST .../cancel` |
| Find current hold | `GET /reservations?status=held&limit=1` |

The date sent to `GET /showtimes` is the calendar day the user picked, a
`YYYY-MM-DD` string; the backend interprets it in the cinema time zone (SPEC-3).
The default picked day is "today" in the **cinema** time zone (`lib/time.ts`).

### 2.5 Behavior rules

- **Seat selection** (pure functions in `seatSelection.ts`): clicking a seat
  toggles it; only `available` seats can be selected; at most 8; when a polled
  map shows a selected seat as no longer `available`, it is removed from the
  selection and the user is told which seat was lost.
- **Hold**: sends `showtime_id` and `seat_ids`. On `201` go to
  `/checkout/{id}`. On `409 SEAT_UNAVAILABLE` refetch the map, drop the lost
  seats and show the message. On `409 ACTIVE_HOLD_EXISTS` show the message with a
  link to the user's current hold (found with `status=held`). On `409
  SHOWTIME_STARTED` disable holding and say the showtime has started. Anonymous
  users are sent to login first and returned to the same showtime.
- **Checkout**: shows seats, `total_cents`, and a countdown to `expires_at`
  (computed from the server value). When the countdown reaches zero the page
  refetches the reservation; the **server's** status decides what is shown
  (`expired` disables Confirm and offers "Choose seats again"). Confirm → `200`
  shows the confirmed state; Release → back to the seat map. A reservation that
  is not `held` never shows Confirm or Release.
- **My reservations**: filters `status` and `upcoming` map directly to the API;
  "Cancel" is shown only for `confirmed` reservations whose showtime is
  upcoming and asks for confirmation first; after success the list is
  refetched. API errors (for example `SHOWTIME_STARTED`) are shown as messages.
- **Money**: integer cents from the API are formatted for display only
  (`lib/money.ts`, via `Intl.NumberFormat`); the UI never does money arithmetic
  except what the API already returns (totals come from `total_cents`).
- **Errors**: every backend error code has a friendly message in
  `errorMessages.ts`; unknown codes fall back to a generic message plus the
  server's `message`. The UI never shows raw JSON or stack traces. Network
  failures show a retry action.
- **Loading / empty / error states** exist on every data screen (skeleton or
  spinner, an empty-state text, an error with Retry).

### 2.6 Backend change: CORS

`core/config.py`: `CORS_ORIGINS` parsed into a list of origins; invalid or `*`
entries refuse to start (same style as the other settings). `main.py`:
`CORSMiddleware` added with the values from D2, installed so that error
responses (the standard error shape) also carry the CORS headers. `.env.example`
documents the setting.

---

## 3. Edge Cases & Constraints

- Token expired or deleted user: the next call returns 401 `UNAUTHENTICATED`;
  the app clears the session and shows login with a short notice.
- Two tabs: a hold made in one tab appears in the other as `held`/`ACTIVE_HOLD_EXISTS`;
  logout in one tab does not break the other tab (next 401 handles it).
- The browser back button after confirming shows the confirmed state, not a
  Confirm button (state comes from the server).
- A seat the user selected is taken by someone else while they decide: removed
  from the selection with a message (FR-4).
- Hold succeeded but navigation was interrupted: the hold is visible in My
  reservations and can be confirmed or released from checkout.
- Large screens (up to 26 rows × 40 seats): the grid scrolls horizontally on
  small devices and keeps row labels visible.
- Missing poster or broken image: a neutral placeholder; `poster_url` is used only
  as an `<img src>`.
- Showtime that has started: the seat map is viewable but holding is disabled.
- A day without showtimes: an empty state, not an error.
- System clock wrong on the client: the countdown may be off, but the server's
  `expired` status is what ends the hold (D6 behavior rule).

---

## 4. Configuration

Backend `.env.example` adds `CORS_ORIGINS`. Frontend `frontend/.env.example`:
`VITE_API_BASE_URL`, `VITE_CURRENCY`, `VITE_CINEMA_TIMEZONE`. No secrets are
ever placed in frontend variables (everything `VITE_*` is public in the bundle).

---

## 5. Testing Requirements

### 5.1 Tests

Backend (CORS), existing test setup:

1. `test_settings_cors_origins_parsing_and_rejects_wildcard` (unit)
2. `test_cors_preflight_allows_configured_origin_and_authorization_header`
3. `test_cors_disallowed_origin_gets_no_allow_origin_header`
4. `test_cors_headers_present_on_error_responses_and_error_shape_unchanged`

Frontend unit (Vitest, no DOM where possible), in `src/**/*.test.ts`:

5. `formatMoney_formats_integer_cents_without_float_errors` — 0, 1, 99, 100,
   100000000, large values
6. `formatShowtime_uses_cinema_timezone_not_browser_timezone` — run under two
   different `TZ` values, same output; UTC late evening shows the next local day
7. `date_helpers_today_in_cinema_timezone_and_day_navigation`
8. `seatSelection_toggle_only_available_seats_max_eight_and_drop_taken`
9. `groupSeatsByRow_sorts_rows_and_numbers`
10. `countdown_remaining_and_expiry_boundary`
11. `errorMessages_cover_every_backend_error_code` — the test lists all codes from
    SPEC-2..SPEC-6 and fails if one is missing
12. `tokenStorage_survives_storage_errors_and_clears`
13. `apiClient_sends_bearer_token_and_parses_standard_error_shape`
14. `apiClient_clears_session_on_401_unauthenticated`

Frontend component/integration (Vitest + React Testing Library + MSW; the API is
mocked with realistic responses taken from the backend schemas):

15. `login_success_stores_token_and_redirects_to_next_path`
16. `login_wrong_credentials_shows_message_and_stores_nothing`
17. `signup_validation_and_duplicate_email_message`
18. `protected_routes_redirect_anonymous_to_login_with_next`
19. `session_restored_from_stored_token_and_invalid_token_cleared`
20. `browse_shows_showtimes_for_selected_date_grouped_by_movie`
21. `browse_empty_day_and_error_with_retry`
22. `movie_page_lists_its_upcoming_showtimes`
23. `seat_map_renders_states_with_accessible_labels`
24. `seat_map_polls_and_removes_selected_seat_that_became_taken`
25. `seat_selection_stops_at_eight_seats`
26. `hold_success_navigates_to_checkout_with_countdown`
27. `hold_conflict_refreshes_map_and_shows_message`
28. `hold_active_hold_exists_links_to_current_hold`
29. `hold_requires_login_and_returns_to_showtime`
30. `started_showtime_disables_hold`
31. `checkout_confirm_success_shows_confirmed_state`
32. `checkout_countdown_expiry_disables_confirm_and_uses_server_status`
33. `checkout_release_returns_to_seat_map`
34. `my_reservations_list_filters_by_status_and_upcoming`
35. `cancel_confirmed_upcoming_asks_confirmation_and_updates_list`
36. `cancel_error_shows_friendly_message`
37. `logout_clears_session_and_redirects`
38. `unknown_route_shows_not_found`

### 5.2 Quality gates

Frontend: `npm run lint`, `npm run typecheck` (`tsc --noEmit`, strict), `npm test`,
`npm run build` all pass locally and in CI. Backend: the full existing suite
stays green (173 tests before this SPEC) plus tests 1-4.

### 5.3 Manual end-to-end acceptance (the real-server smoke test)

Run with the real backend (`python -m src.main`) and the real frontend
(`npm run dev`), against the dev database. Record the result in the
Implementation Status.

1. Sign up a new user; reload the page; still logged in.
2. Log out; log in again; wrong password shows a friendly error.
3. Browse today's showtimes (an admin seeds a movie/showtime via the API docs
   page `/docs` first); open a movie page.
4. Open a showtime; the seat map matches the screen.
5. Select 3 seats; Hold; checkout shows the countdown and the right total.
6. In a second browser (or private window) as another user: the same seats show
   as `held`; trying to hold them gives the conflict message.
7. Confirm in the first browser; seats show as `taken` in the second.
8. My reservations lists the booking; Cancel it; the seats become `available` in
   the second browser within about 5 seconds.
9. Let a hold expire (or shorten `HOLD_MINUTES` in the dev `.env` to 1 for this
   check): checkout shows expired; the seats can be held by the other user.
10. Make a second hold while one is active: the "current hold" link works.
11. Open the app with the browser time zone set to something else (for example
    New York): showtime times still show in the cinema time zone.
12. Resize to a phone width: seat map scrolls, nothing is cut off.
13. Switch the system to dark mode: readable, no invisible text.

---

## 6. Acceptance Criteria (EARS)

  FR-1  WHEN a user signs up or logs in successfully, THE SYSTEM SHALL keep the
        session across page reloads; IF a request returns 401 `UNAUTHENTICATED`,
        THE SYSTEM SHALL clear the session and send the user to login; protected
        routes SHALL redirect anonymous users to login and return them afterwards.
  FR-2  THE SYSTEM SHALL let anyone browse showtimes for a chosen calendar day
        (default: today in the cinema time zone) and open a movie page with its
        upcoming showtimes.
  FR-3  THE SYSTEM SHALL show a showtime's seat map with each seat as available,
        held or taken, refresh it every 5 seconds while the tab is visible, and
        expose each seat to assistive technology with its row, number and state.
  FR-4  THE SYSTEM SHALL allow selecting only available seats, at most 8, and
        SHALL remove from the selection, with a message, any selected seat that
        becomes unavailable.
  FR-5  WHEN a logged-in user holds seats, THE SYSTEM SHALL open the checkout
        page on success; IF the hold is refused (`SEAT_UNAVAILABLE`,
        `ACTIVE_HOLD_EXISTS`, `SHOWTIME_STARTED`, `TOO_MANY_SEATS`), THE SYSTEM
        SHALL show the matching message and keep the app usable (refreshed map,
        link to the current hold).
  FR-6  THE SYSTEM SHALL show the hold's countdown from the server's `expires_at`,
        offer Confirm and Release only for a `held` reservation, and decide what to
        show from the server's status after the countdown ends.
  FR-7  THE SYSTEM SHALL list the user's reservations with the API's `status` and
        `upcoming` filters, and offer Cancel (after a confirmation prompt) only for
        confirmed, upcoming reservations.
  FR-8  THE SYSTEM SHALL show a friendly message for every error code of SPEC-2 to
        SPEC-6 and a generic message for unknown ones, and SHALL NOT show raw JSON
        or stack traces.
  FR-9  THE SYSTEM SHALL accept browser requests from the configured
        `CORS_ORIGINS` only, including the `Authorization` header and error
        responses, and SHALL refuse to start with a wildcard origin.
  FR-10 THE SYSTEM SHALL display money from integer cents in the configured
        currency and every time in the cinema time zone, independent of the
        browser's time zone.
  FR-11 THE SYSTEM SHALL render server text as plain text (no raw HTML), store the
        token only through the token-storage module, use `poster_url` only as an
        image source, accept only same-origin relative `next` paths, and put no
        secrets in frontend variables.
  FR-12 THE SYSTEM SHALL be usable on a phone width, in dark mode, and with a
        keyboard (seat buttons focusable, state not conveyed by color alone).
  FR-13 THE SYSTEM SHALL pass lint, strict typecheck, tests and build locally and
        in CI, and the backend suite SHALL stay green.
  FR-14 THE SYSTEM SHALL show loading, empty and error-with-retry states on every
        screen that loads data.

### 6.1 Requirements Mapping

| Requirement | Design decision / section | Tests |
|---|---|---|
| FR-1 | D3, 2.3 | 12, 14, 15, 16, 17, 18, 19, 29, 37 |
| FR-2 | 2.3, 2.4, D5 | 7, 20, 21, 22 |
| FR-3 | D6, 2.5 | 9, 23, 24 |
| FR-4 | 2.5 seat selection | 8, 24, 25 |
| FR-5 | 2.5 hold | 26, 27, 28, 29, 30 |
| FR-6 | 2.5 checkout | 10, 31, 32, 33 |
| FR-7 | 2.5 my reservations | 34, 35, 36 |
| FR-8 | D4, 2.5 errors | 11, 13, 16, 36 |
| FR-9 | D2, 2.6 | 1, 2, 3, 4 |
| FR-10 | D5, 2.5 money | 5, 6, 7 |
| FR-11 | D3, 3 | 12, 18, 22 |
| FR-12 | D9, 3 | 23, 25, 5.3 steps 12-13 |
| FR-13 | D10, 5.2 | CI |
| FR-14 | 2.5 | 21, 38 |

### Equivalence Partitioning + Boundary Value Analysis

Seats selected (FR-4): 0 (Hold disabled), 1, 8 (accepted), 9th seat ignored with a
message.
Countdown (FR-6): remaining > 0 shows time; remaining = 0 triggers refetch and the
server status wins; 1 s before and at expiry are both tested.
Money (FR-10): 0, 1, 99, 100, 100 000 000 cents.
Showtime near local midnight (FR-10): 23:30 and 00:30 Tbilisi appear under their
Tbilisi dates whatever the browser's `TZ`.
`next` path (FR-11): `/showtimes/1` accepted; `https://evil.example`, `//evil.example`
and `javascript:` rejected.

---

## 7. Logging and Privacy

- No analytics, no third-party scripts, no logging of tokens, passwords or
  response bodies to the console in production builds.
- `console.error` is allowed only in development builds.

---

## 8. Reuse/Dependency Check

New runtime dependency (sign-off with SPEC approval): `react-router-dom` (the
standard router, maintained, MIT). New dev-only dependencies: `vitest`,
`@testing-library/react`, `@testing-library/user-event`,
`@testing-library/jest-dom`, `msw`, `eslint`, `typescript-eslint`, `prettier`.
Backend: no new dependency (Starlette CORS is already installed).

Reused unchanged: the whole backend API and the standard error shape.

---

## 9. Non-Goals / Revisit

Out of scope: the admin UI (SPEC-8), Georgian/other languages, payment, seat-map
animations, PWA/offline, OpenAPI client code generation, Playwright end-to-end
automation, SEO/server-side rendering, analytics, WebSockets.

Revisit if: the hand-written API types drift from the backend (generate them from
`/openapi.json`); an automated browser end-to-end suite is wanted (add Playwright
as its own SPEC); the app is deployed publicly (choose hosting, HTTPS, strict
CORS origins, a Content-Security-Policy, and reconsider the `localStorage` token).

---

## 10. DRY Check

- Error code to message mapping: only `lib/errorMessages.ts`.
- Money and time formatting: only `lib/money.ts` and `lib/time.ts`.
- API base URL, currency and time zone: only from `import.meta.env` read in one
  config module.
- HTTP calls and error parsing: only `api/client.ts`.
- Token access: only `lib/tokenStorage.ts`.
- Seat grouping and selection rules: only `features/seats/seatSelection.ts`.

## 11. Single Responsibility Check

- `api/*`: network and response types only.
- `lib/*`: pure helpers, no React.
- `features/*`: screens and their hooks; no `fetch` calls.
- `components/ui/*`: presentational building blocks, no business rules.

## 11.1 Layer & Dependency Check

`features` -> `api`, `lib`, `components/ui`; `api` -> `lib`; `lib` -> nothing in
the project; `components/ui` -> `lib` only. No feature imports another feature
except through shared `api/` or `lib/`.

---

## Amendment Log

(none yet)

---

## Implementation Status

NOT STARTED
