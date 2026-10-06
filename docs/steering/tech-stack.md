# Tech Stack — Movie Reservation System

| Choice | Why |
|---|---|
| **Python 3.13** | Matches primary stack; team's existing tooling and habits. |
| **FastAPI** | Async-native, typed request/response models via Pydantic, functional route handlers (no MVC-class ceremony) — fits functional-style preference. |
| **PostgreSQL** | Reservation records need real ACID guarantees and row-level locking (`SELECT ... FOR UPDATE`) to prevent seat double-booking under concurrent requests — SQLite's write-locking model can't safely support this. |
| **SQLAlchemy Core** (not ORM), installed as `sqlalchemy[asyncio]` (pulls in `greenlet`, which the async engine requires) | Data access as explicit, composable functions operating on `Table`/`select`/`insert` constructs, not model classes with hidden state and cascade magic — matches the functional-over-OOP preference. Constraints (uniqueness, foreign keys, cascades) are declared in the schema, not inferred from model relationships. |
| **Alembic** | Schema migrations, versioned and reviewable — standard pairing with SQLAlchemy. |
| **Pydantic v2** | Request/response validation and serialization; comes bundled with FastAPI. |
| **PyJWT + bcrypt (used directly)** | JWT-based auth (stateless, no session store needed); bcrypt for password hashing. `bcrypt` is called directly (`hashpw` / `checkpw`, about ten lines) instead of through `passlib`: passlib's last release was in 2020 and its bcrypt backend fails against bcrypt 5.x (found in PROMPT 4). bcrypt rejects passwords longer than 72 bytes, so the auth module must validate a maximum password length of 72 bytes (UTF-8) before hashing. |
| **pytest + httpx (ASGI test client)** | Unit and integration tests against the FastAPI app without a running server. |
| **Docker Compose** | Postgres + app for reproducible local dev; avoids "works on my machine" for a project with real concurrency behavior to test. |
| **ruff, mypy --strict, bandit, radon** | Automated Quality Score Gate per SDD workflow. |

## Deliberate exclusions

- **No ORM relationships/cascades** — cascade behavior (e.g. delete a
  showtime → what happens to its reservations) is enforced by explicit SQL
  `ON DELETE` clauses in the schema, not inferred by an ORM, so the rule is
  visible in one place (the migration) rather than split between the schema
  and model classes.
- **No Celery/background task queue** — the payment-confirm step is
  synchronous (see SPEC-4); no need for async job infrastructure at this
  scale.
- **No Redis** — seat-hold concurrency is handled by PostgreSQL row locking
  and a unique constraint, not a separate locking service. Revisit if this
  needs to scale past a single DB instance.

## Revisit if

- Concurrent write volume grows past what a single Postgres instance
  handles comfortably → consider connection pooling tuning (pgbouncer)
  before reaching for a different concurrency strategy.
- A real payment provider is added → the stubbed confirm step becomes the
  seam where that integration plugs in (see SPEC-4 extension points).
