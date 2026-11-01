# Frontend — Movie Reservation System user app

The customer-facing single-page app from SPEC-7: sign up / log in, browse
showtimes by date, a movie page, the seat map, hold, checkout, confirm or
release, and "My reservations". It uses the backend API in `backend/` and
nothing else.

React + TypeScript (strict) + Vite, Tailwind CSS + shadcn/ui primitives,
TanStack Query for server state, React Router for routing, and Vitest + React
Testing Library + MSW for tests.

## Requirements

- Node.js 24 (see `.nvmrc`) and npm.
- For the end-to-end checklist at the bottom: the backend running as well
  (Python 3.13, a virtualenv, and a Postgres database). Its settings live in
  `backend/.env.example`.

## Install

Run these from the repository root, one command per line:

```powershell
cd frontend
npm install
Copy-Item .env.example .env
```

`Copy-Item` is only needed once; `.env` is git-ignored. Skip it if the file is
already there and edit the existing one instead.

## Run: backend and frontend together

Two terminals, both started in the repository root.

Terminal 1, the backend (its commands run from `backend/`, SPEC-2):

```powershell
cd backend
docker compose up -d db
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
alembic upgrade head
python -m src.db.seed
python -m src.main
```

- `docker compose up -d db` starts the bundled local Postgres
  (`backend/docker-compose.yml`). Skip it when `DATABASE_URL` in
  `backend/.env` points at a hosted database.
- `python -m venv` and `pip install -e .` are only needed once; skip them (and
  `Copy-Item`) when `backend/.venv` and `backend/.env` already exist.
- Fill in `DATABASE_URL`, `JWT_SECRET` and `SEED_ADMIN_*` in `backend/.env`
  before `alembic upgrade head` and the seed.
- `python -m src.db.seed` creates the first admin account.
  `python -m src.db.seed_demo` is optional: it also seeds demo movies,
  showtimes and posters.
- Use `python -m src.main`, not `uvicorn src.main:app --reload`: on Windows
  only that entry point selects the event loop async psycopg needs
  (SPEC-2 Section 2.8).

The API answers at http://127.0.0.1:8000 and its docs page is at
http://127.0.0.1:8000/docs.

Terminal 2, the frontend:

```powershell
cd frontend
npm run dev
```

The app runs at http://localhost:5173 and calls the backend at
`VITE_API_BASE_URL` (default `http://127.0.0.1:8000`). The backend's default
`CORS_ORIGINS` already allows that origin.

## Quality gates

From `frontend/`, on the Node version in `.nvmrc`:

```powershell
npm run lint
npm run typecheck
npm test
npm run build
```

`npm test` runs Vitest with MSW, so it needs no server and no network.

## Environment

Copy `.env.example` to `.env` and adjust if needed. Every `VITE_*` variable is
public in the bundle, so never put a secret in one.

| Variable               | Default                 | Purpose                              |
| ---------------------- | ----------------------- | ------------------------------------ |
| `VITE_API_BASE_URL`    | `http://127.0.0.1:8000` | Backend API base URL                 |
| `VITE_CURRENCY`        | `GEL`                   | Currency used to format money        |
| `VITE_CINEMA_TIMEZONE` | `Asia/Tbilisi`          | Time zone all showtimes are shown in |

Only `src/lib/config.ts` reads these.

## Scripts

| Script              | Command                                | Purpose                         |
| ------------------- | -------------------------------------- | ------------------------------- |
| `npm run dev`       | `vite`                                 | Development server              |
| `npm run build`     | `tsc -b && vite build`                 | Type check and production build |
| `npm run lint`      | `eslint .`                             | Lint                            |
| `npm run typecheck` | `tsc --noEmit` (app and node projects) | Strict type check               |
| `npm test`          | `vitest run`                           | Unit and component tests        |
| `npm run format`    | `prettier --write .`                   | Format the project              |
| `npm run preview`   | `vite preview`                         | Serve the production build      |

## Folder layout

```
frontend/
  package.json  package-lock.json  .nvmrc  index.html  vite.config.ts
  tsconfig.json  tsconfig.app.json  tsconfig.node.json  eslint.config.js
  .prettierrc  components.json  .env.example
  public/          favicon and the seeded demo posters (/posters/*.svg)
  src/
    main.tsx  App.tsx             providers (QueryClient, Router, Auth) and routes
    AppShell.tsx  NotFoundPage.tsx  index.css
    api/            client.ts  types.ts  auth.ts  catalog.ts  seats.ts  reservations.ts
    features/
      auth/         LoginPage  SignupPage  AuthContext  RequireAuth  authForm
      browse/       BrowsePage  MoviePage  DayStrip  ShowtimeCard  MoviePoster
                    LoadError  queries  showtimeGroups
      seats/        SeatMapPage  SeatGrid  seatSelection
      reservations/ CheckoutPage  MyReservationsPage  ReservationCard
    components/
      layout/       content-wrapper
      ui/           shadcn/ui: button  alert  alert-dialog  badge  input  label  skeleton
    lib/            config  money  time  countdown  errorMessages  tokenStorage
                    safeNextPath  utils
    test/           setup.ts (jest-dom, MSW lifecycle)  server.ts (handlers)
```

Components never call `fetch`: they use `api/` through TanStack Query hooks, and
`api/types.ts` mirrors the backend response models (snake_case fields).
`lib/config.ts` is the only reader of `VITE_*` variables; `lib/tokenStorage.ts`
is the only reader and writer of the stored token. Tests sit next to the code
they cover (`*.test.ts`, `*.test.tsx`).

## Manual end-to-end acceptance (SPEC-7 Section 5.3)

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
