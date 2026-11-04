# SPEC-7B: Free Public Deployment

Feature:      Publish the app for free: frontend on Cloudflare Pages, backend on
              Render (free web service), a separate production database on Neon,
              filled with the showcase data
Priority:     P6 (Showcase)
Status:       See SPEC-7B.status — this line is for human reference only
Dependencies: SPEC-7 and SPEC-7A implemented; backend CORS (SPEC-7 FR-9)
Related Docs: SPEC-2 (config, startup), SPEC-7, SPEC-7A, steering/*.md

---

## 1. Overview

The finished app runs only on George's computer. This SPEC puts it on the public
internet at no cost, as a portfolio piece. Nothing about business behavior
changes. The work is: a health endpoint, a Render blueprint file, a static-hosting
routing file, a "server is waking up" notice for the free tier's cold start, and
a written, repeatable deployment procedure.

Known limits of the free setup (accepted): the Render free service sleeps after
15 minutes without traffic and needs about a minute to wake; local files are lost
on restart (the app writes none); Render free has no shell and no pre-deploy
command, so migrations and seeding run from George's computer against the
production database.

Out of scope: custom domain, paid plans, monitoring beyond a ping, the admin UI
(SPEC-8), logging services.

---

## 2. Design

### 2.1 Decisions (need George's approval)

| # | Decision | Reason |
|---|---|---|
| D1 | Frontend: Cloudflare Pages, connected to the GitHub repo, root directory `frontend`, build `npm run build`, output `dist`, variable `NODE_VERSION=24`, build variable `VITE_API_BASE_URL` | Free, never sleeps, deploys on push |
| D2 | Backend: Render free web service from the same repo, described by `render.yaml` at the repo root (root directory `backend`, build `pip install .`, start `uvicorn src.main:app --host 0.0.0.0 --port $PORT`, health check `/health`) | Free, no card, Docker not needed |
| D3 | Production database: a NEW Neon database `movie_reservation_prod` (not the dev or test one) | Public demo must not touch dev data |
| D4 | Migrations (`alembic upgrade head`) and data (`python -m src.db.seed`, `python -m src.db.seed_demo`) run on George's computer with `DATABASE_URL` set only for that PowerShell session | Render free has no shell; the URL never enters a file or git |
| D5 | New secrets for production: a fresh `JWT_SECRET` (random, at least 32 characters), a new admin (`SEED_ADMIN_*`) and a new demo password; set only in the Render dashboard or in the session for seeding | Dev secrets are never reused |
| D6 | `GET /health` returns `{"status":"ok"}` with status 200, needs no database and no authentication; added to the backend with one test | Render health check and the ping use it |
| D7 | Keep-awake ping: a free external monitor (UptimeRobot) calls `/health` every 5 minutes | One free service fits in 750 hours a month; makes the site usually awake |
| D8 | Cold-start notice: when a data request on the browse page takes longer than 4 seconds, show the line "Waking up the server. This can take up to a minute." in the loading state; it disappears when data arrives | A visitor must understand the wait |
| D9 | `frontend/public/_redirects` with `/* /index.html 200` so a reload of `/movies/3` works | Single-page app on static hosting |
| D10 | `CORS_ORIGINS` on Render = the Pages address exactly (scheme + host, no slash); `DEMO_POSTER_BASE_URL` for the seed = the same address | Posters and CORS use the public address |
| D11 | Deploys happen automatically on push to `master` on both hosts | Simple; CI still runs on every push |

### 2.2 Environment variables

Render (dashboard, "environment"): `DATABASE_URL` (production Neon, asyncpg form
as used by the app), `JWT_SECRET`, `CORS_ORIGINS`, `CINEMA_TIMEZONE`
(Asia/Tbilisi), `PYTHON_VERSION`. Cloudflare Pages: `VITE_API_BASE_URL` (the
Render address, no trailing slash), `VITE_CURRENCY` (GEL), `VITE_CINEMA_TIMEZONE`
(Asia/Tbilisi), `NODE_VERSION`. No secret goes into any `VITE_*` variable.

### 2.3 Order of work (important)

1. Create the production database; run migrations and seeds from the computer
   (needs the final Pages address for poster URLs, so reserve the Pages project
   name first and note its address).
2. Deploy the backend; check `/health` and `/docs`.
3. Deploy the frontend with `VITE_API_BASE_URL`; then set `CORS_ORIGINS` on Render
   to the Pages address and redeploy the backend if it was set later.
4. Add the ping. 5. Run the smoke checklist.

---

## 3. Requirements (EARS)

- **FR-1** When `GET /health` is called, the system shall answer 200 with
  `{"status":"ok"}` without opening a database connection.
- **FR-2** The repository shall contain `render.yaml` that creates the backend
  service as in D2, with secrets declared as unsynced environment variables and
  no secret value in the file.
- **FR-3** The frontend build shall include `_redirects` so any path serves the
  app.
- **FR-4** While the browse page's data request is pending for more than 4
  seconds, the system shall show the waking-up line; when the request finishes
  or fails, the line shall disappear.
- **FR-5** The documentation shall describe every manual step, in order, as
  copy-paste PowerShell commands, with no secret value.
- **FR-6** The README shall have a "Live demo" section with the public address and
  the demo account email (the password is shared only on request).
- **FR-7** No change shall alter any existing API behavior or existing test.

---

## 4. Requirements Mapping

| FR | Covered by |
|---|---|
| FR-1 | Test 1 |
| FR-2 | Review of `render.yaml` (no secrets); smoke S2 |
| FR-3 | Smoke S5 (reload on a deep link) |
| FR-4 | Test 2 |
| FR-5, FR-6 | Review |
| FR-7 | Existing suites stay green |

---

## 5. Verification

### 5.1 Automated
1. `health_returns_ok_without_database` (backend) — 200, exact body, works with the
   database dependency overridden to fail.
2. `browse_shows_waking_up_notice_after_four_seconds_and_hides_it` (frontend, fake
   timers, MSW delayed response) — absent at 3.9 s, present at 4.1 s, gone after
   data arrives; also gone after an error.

All existing tests stay green: 182 backend, 40 frontend plus test 2.

### 5.2 Boundaries
Notice: 3.9 s, 4.0 s, 4.1 s. Health: with and without database reachable.

### 5.3 Smoke checklist (George, on the public addresses)
- S1 `https://<render-address>/health` shows `{"status":"ok"}`.
- S2 `https://<render-address>/docs` opens.
- S3 The Pages address opens; browse shows movies with posters (first load may
  show the waking-up line).
- S4 Sign up with a new email; log in and out.
- S5 Open a movie, copy its address, open it in a new tab and reload: it works.
- S6 Pick a showtime, hold seats, confirm; it appears in My reservations.
- S7 Cancel that booking; the seats become free on the seat map.
- S8 Log in as the demo user; three reservations are listed.
- S9 Browser console has no CORS errors.
- S10 After 20 minutes of no visits, open the site: the notice appears, then the
  data (cold start), or the ping kept it awake.

---

## 6. Steps
PROMPT 7B-1 (health endpoint, `render.yaml`, test 1), PROMPT 7B-2 (`_redirects`,
waking-up notice, test 2, README live-demo placeholder). Then George follows
the procedure step by step with Claude (accounts, database, variables, seeding,
ping).

## 7. Risks
- Wrong `CORS_ORIGINS` (trailing slash or wrong host) blocks the whole site: S9.
- Production seed with wrong poster base URL gives broken posters: D10; re-seeding
  is not possible (idempotent), so check the address before seeding.
- A secret committed by mistake: D4, D5, FR-2, review before every commit.
- Free-tier limits change: re-check each host's page before starting.

## 8. Dependencies
None new in code. External services: Cloudflare Pages, Render, UptimeRobot, Neon.

---

## Amendment Log

(none yet)

---

## Implementation Status

NOT STARTED
