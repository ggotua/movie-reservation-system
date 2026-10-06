# Conventions — Movie Reservation System

## Style guide

Python code follows **PEP 8**, enforced by `ruff` (formatting + linting) and
`mypy --strict` (typing). No deliberate deviations at project start; any
future deviation must be recorded here with a reason.

## Project layout (modular monolith)

```
docs/            # SDD documents (steering, specs, prompts)
frontend/        # React + Vite + Tailwind + shadcn/ui (added after SPEC-4)
backend/
  pyproject.toml, alembic.ini, docker-compose.yml, .env.example
  src/
    auth/          # signup, login, JWT issuance, role checks
    movies/        # movie + genre CRUD (admin)
    showtimes/     # showtime scheduling (admin), showtime browsing (public)
    reservations/  # seat selection, hold/confirm, cancel, list-mine
    reporting/     # admin: capacity, revenue, reservation listing
    db/            # SQLAlchemy Core table definitions, engine/session setup
    core/          # shared: config, security (hashing/JWT), error types
  tests/
    unit/
    integration/
  migrations/      # Alembic
```

All backend commands (venv, pytest, alembic, uvicorn) run from `backend/`;
Python imports stay `src.<module>` (no `backend.` prefix).

Each module under `backend/src/` owns one responsibility and exposes a small
functional interface (plain functions taking/returning typed data, not
classes with hidden state) that the FastAPI route layer calls into. Route
handlers stay thin: parse/validate request → call a module function →
shape the response. Business rules live in the module, not the route
handler or the DB table definition (Layer & Dependency Check).

## Naming

- Tables: `snake_case`, plural (`users`, `showtimes`, `seat_reservations`)
- Python functions/variables: `snake_case`; Pydantic models: `PascalCase`
  suffixed by role (`ReservationCreate`, `ReservationOut`)
- Money fields: `*_cents` suffix, always `int` (e.g. `price_cents`)
- Timestamps: `*_at` suffix, always UTC, `timestamptz` in Postgres

## Error shape

All API errors return a consistent JSON body:

```json
{
  "error": {
    "code": "SEAT_ALREADY_RESERVED",
    "message": "Seat B12 is no longer available for this showtime."
  }
}
```

- `code` is a stable, machine-readable `SCREAMING_SNAKE_CASE` string —
  clients branch on this, not on `message` text.
- `message` is human-readable and safe to show a user.
- HTTP status communicates the category (400 validation, 401
  unauthenticated, 403 unauthorized, 404 not found, 409 conflict — used for
  the seat-race case specifically, 422 Pydantic validation errors).

## Auth at the boundary

- JWT bearer token on every authenticated route, verified by a shared
  FastAPI dependency (`get_current_user`).
- A second dependency (`require_admin`) wraps `get_current_user` for
  admin-only routes — authentication and authorization are always two
  separate checks (see `principles.md`).
- Ownership checks (a user can only cancel *their own* reservation) happen
  inside the reservations module function, not just at the route layer, so
  the rule holds even if called from another module later.

## Testing

- Every module gets unit tests for its pure logic (no DB, no HTTP) plus
  integration tests that hit a real test Postgres instance via the ASGI
  test client.
- The seat-reservation race condition gets a dedicated concurrency test
  (see SPEC-4) — this is the one piece of business logic where "the code
  looks right" is not sufficient evidence.
