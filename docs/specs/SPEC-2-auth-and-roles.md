# SPEC-2: Auth & Roles

Feature:      Signup, login, JWT, role checks, admin promotion, API skeleton
Priority:     P1 (Foundation)
Status:       See SPEC-2.status — this line is for human reference only
Dependencies: SPEC-1 (users table)
Related Docs: APP-OVERVIEW.md, steering/tech-stack.md, steering/conventions.md,
              steering/principles.md, specs/SPEC-1-database-schema.md

---

## 1. Overview

First HTTP-facing SPEC. It delivers:

1. The minimal FastAPI application skeleton every later SPEC plugs into
   (app factory, database connection dependency, standard error shape).
2. Account creation and login (email + password, JWT bearer tokens).
3. The two shared authorization dependencies, `get_current_user` and
   `require_admin`, that SPEC-3 to SPEC-6 use unchanged.
4. Admin promotion of another user to `admin`.
5. Resolution of the temporary hashing duplication left in `seed.py`: one
   password-hashing implementation in `src/core/security.py`, used by both
   `auth` and the seed script (DRY, `principles.md`).

This SPEC refines SPEC-1 Section 2.1 validation rules (password maximum of
72 bytes, email normalization). No schema change; no SPEC-1 amendment needed.

Technology: FastAPI, Pydantic v2, PyJWT (HS256), bcrypt (direct),
SQLAlchemy Core (async), httpx `ASGITransport` for API tests.

---

## 2. Design

### 2.1 Module layout

```
backend/src/
  core/
    config.py      # + JWT_SECRET minimum length rule (FR-10)
    errors.py      # ApiError(code, message, status_code) + handlers' data
    security.py    # hash_password, verify_password, create_access_token,
                   # decode_access_token  (pure functions, no DB)
  db/
    engine.py      # + get_connection FastAPI dependency (one transaction
                   #   per request: commit on success, rollback on error)
  auth/
    schemas.py     # SignupRequest, LoginRequest, UserOut, TokenOut
    service.py     # normalize_email, validate_password, signup, authenticate,
                   # get_user_by_id, promote_to_admin  (take a connection)
    dependencies.py# get_current_user, require_admin
    router.py      # thin route handlers
  main.py          # create_app(), error handlers, run() entry point
```

Business rules live in `auth/service.py` and `core/security.py`. Route
handlers only parse, call, and shape the response (`conventions.md`).

### 2.2 Endpoints

| Method + path | Auth | Success | Errors |
|---|---|---|---|
| `POST /auth/signup` | none | 201 `UserOut` | 409 `EMAIL_ALREADY_REGISTERED`, 422 `VALIDATION_ERROR` |
| `POST /auth/login` | none | 200 `TokenOut` | 401 `INVALID_CREDENTIALS`, 422 `VALIDATION_ERROR` |
| `GET /auth/me` | bearer | 200 `UserOut` | 401 `UNAUTHENTICATED` |
| `POST /admin/users/{user_id}/promote` | bearer, admin | 200 `UserOut` | 401, 403 `FORBIDDEN`, 404 `USER_NOT_FOUND` |

Bodies are JSON (not OAuth2 form data). Swagger UI uses the `HTTPBearer`
scheme: log in via `/auth/login`, paste the token into "Authorize".

```
SignupRequest { email: str, password: str }          # extra fields forbidden
LoginRequest  { email: str, password: str }          # extra fields forbidden
UserOut       { id: int, email: str, role: str, created_at: datetime }
TokenOut      { access_token: str, token_type: "bearer", expires_in: int }  # seconds
```

`UserOut` never contains `password_hash`.

### 2.3 Input rules (enforced in the `auth` module)

- **Email:** stripped, lowercased (`normalize_email`) before any lookup or
  insert; must be a syntactically valid address; at most 254 characters.
  Lookups compare `LOWER(email)` so they match the SPEC-1 functional unique
  index regardless of how an old row was stored.
- **Password:** at least 8 characters and at most 72 bytes in UTF-8
  (bcrypt ignores or rejects anything longer; `tech-stack.md`). Counting
  bytes matters: one Georgian letter is 3 bytes, so 24 Georgian letters is
  the longest allowed password made only of them. Only the bcrypt hash is
  stored. A password over 72 bytes at login is simply `INVALID_CREDENTIALS`.
- **Mass assignment:** request models forbid extra fields, so a client
  sending `"role": "admin"` at signup gets a 422 and cannot self-promote.
  New accounts are always `role = 'user'`.

### 2.4 Tokens

- JWT, HS256 via PyJWT. Claims: `sub` (user id as string), `iat`, `exp`.
  **The role is not in the token.** `get_current_user` loads the user row
  per request, so a promotion (or a deleted account) takes effect
  immediately and a token never carries a stale role. Cost: one indexed
  primary-key lookup per authenticated request, acceptable at this scale.
- Lifetime: `JWT_EXPIRY_MINUTES` (existing setting, default 60).
- Decoding pins `algorithms=["HS256"]` and requires `exp` and `sub`; the
  `none` algorithm and any other algorithm are rejected.
- `JWT_SECRET` must be at least 32 characters; shorter values stop startup
  (extends the existing "fail loudly if unset" rule).
- No refresh tokens and no logout/revocation in v1 (a token is valid until
  it expires). See Section 9.

### 2.5 Authentication vs authorization

Two separate dependencies, as in `conventions.md`:

- `get_current_user`: bearer token, verified, user loaded -> else 401.
- `require_admin`: wraps `get_current_user`; role must be `admin` -> else 403.

An unauthenticated request to an admin route returns 401 (authentication
fails first), never 403.

### 2.6 Login does not reveal which half was wrong

Unknown email and wrong password return the identical 401 body. For an
unknown email the code still runs one bcrypt verification against a fixed
dummy hash, so response time does not distinguish the two cases.

### 2.7 Database connection per request

`get_connection` (in `src/db/engine.py`) opens one `AsyncConnection` with a
transaction per request: commit if the handler returns normally, rollback on
any exception. Module functions receive the connection as a parameter; they
never open their own and never commit. Tests override this dependency (Section 5).

### 2.8 Windows event loop (closes the open item from PROMPT-4c)

Async psycopg cannot run on Windows' default `ProactorEventLoop`, and uvicorn
selects that loop on Windows. Therefore:

- Local server entry point is `python -m src.main` (run from `backend/`),
  which starts uvicorn programmatically on a `SelectorEventLoop` when
  `sys.platform == "win32"`. `uvicorn src.main:app --reload` is not supported
  on Windows; on Linux (CI, deployment) either form works.
- API tests are async (pytest-asyncio). On Windows `tests/conftest.py`
  overrides pytest-asyncio's `event_loop_policy` fixture to the selector
  policy. The SPEC-1 schema tests stay synchronous and unaffected.

### 2.9 Error shape

All errors from this SPEC use the shape in `conventions.md`:
`{"error": {"code": "...", "message": "..."}}`. FastAPI's own request
validation errors are re-shaped to `code: "VALIDATION_ERROR"` with status 422
and a `message` summarising the first problem (no stack traces, no echoing of
the submitted password). Unauthenticated responses carry
`WWW-Authenticate: Bearer`.

---

## 3. Edge Cases & Constraints

- **Concurrent signup with the same email:** a pre-check alone is racy. The
  insert is attempted and the DB unique violation on `idx_users_email_lower`
  is translated to 409; exactly one request wins (FR-2).
- **Email differing only in case/whitespace** (`A@x.com` vs ` a@x.com`): same
  account.
- **Token for a deleted user:** 401, not 500.
- **Token whose user was promoted after issue:** next request sees `admin`.
- **Promote an already-admin user:** idempotent 200, no error, no second
  audit-worthy change.
- **Promote yourself** (admin promoting own id): allowed, same idempotent rule.
- **Very long inputs:** bounded by the email and password limits above; a
  megabyte password is rejected by Pydantic before hashing.
- **No demotion** endpoint in v1 (Section 9).

---

## 4. Seed script change (DRY)

`backend/src/db/seed.py` currently contains its own bcrypt call (flagged as
temporary duplication). After this SPEC it imports `hash_password` and
`normalize_email` from `src/core/security.py`. `normalize_email` lives in
`core` (not in `auth`) so that `db` never depends on `auth` (Section 11.1).
Seed behaviour is otherwise unchanged and must stay idempotent; the seeded
admin's email is stored normalized (stripped, lowercased).

**Testability refactor.** Today `run_seed()` builds its engine from
`DATABASE_URL` (the development database), so a test could not run it safely.
The seeding logic moves into `seed_database(conn, admin_email, admin_password)`
which takes an open connection and the credentials as parameters and never
reads settings or creates an engine. `run_seed()` stays the CLI entry point:
it reads settings and `SEED_ADMIN_*`, opens the connection, calls
`seed_database`, and commits. Test 29 calls `seed_database` directly with a
connection to `TEST_DATABASE_URL`, so it can never touch the dev database.

---

## 5. Testing Requirements

Unit (no DB, no HTTP), `tests/unit/`:

1. `test_normalize_email_strips_and_lowercases`
2. `test_hash_and_verify_password_roundtrip`
3. `test_verify_password_rejects_wrong_password`
4. `test_verify_password_rejects_password_over_72_bytes_without_raising`
5. `test_password_policy_boundaries` — lengths/bytes 7, 8 (ok), 72 bytes (ok),
   73 bytes (rejected); 24 vs 25 Georgian letters (72 vs 75 bytes)
6. `test_token_roundtrip_returns_subject`
7. `test_decode_rejects_expired_token`
8. `test_decode_rejects_bad_signature`
9. `test_decode_rejects_alg_none_token`
10. `test_decode_rejects_token_missing_sub_or_exp`
11. `test_settings_reject_short_jwt_secret`

Integration (real Postgres via `TEST_DATABASE_URL`, API via httpx
`ASGITransport`, `get_connection` overridden to a per-test rolled-back
transaction), `tests/integration/test_auth_api.py`:

12. `test_signup_creates_user_with_role_user_and_no_hash_in_response`
13. `test_signup_stores_bcrypt_hash_and_lowercased_email`
14. `test_signup_duplicate_email_different_case_returns_409`
15. `test_signup_invalid_email_returns_422`
16. `test_signup_password_boundaries_return_422_outside_policy`
17. `test_signup_with_role_field_returns_422_and_creates_nothing`
18. `test_concurrent_signup_same_email_exactly_one_succeeds` — two simultaneous
    requests on separate committed connections; cleans up its rows in a
    `finally` (this is the one test that cannot use rollback isolation)
19. `test_login_success_returns_token_valid_for_configured_lifetime`
20. `test_login_wrong_password_and_unknown_email_return_identical_401`
21. `test_me_with_valid_token_returns_current_user`
22. `test_me_without_token_or_with_garbage_token_returns_401`
23. `test_me_with_expired_token_returns_401`
24. `test_me_with_token_of_deleted_user_returns_401`
25. `test_promote_requires_admin_returns_403_for_user_and_401_for_anonymous`
26. `test_promote_makes_target_admin_and_takes_effect_on_existing_token`
27. `test_promote_is_idempotent_and_unknown_user_returns_404`
28. `test_error_bodies_use_standard_shape`
29. `test_seed_uses_shared_hashing_and_stays_idempotent` — calls
    `seed_database(conn, ...)` twice on a `TEST_DATABASE_URL` connection (the
    existing `_test` guard applies); asserts one admin with a lowercased
    email whose hash verifies with `verify_password`, and unchanged row counts
    after the second call

---

## 6. Acceptance Criteria (EARS)

  FR-1  WHEN a valid signup request is received, THE SYSTEM SHALL create a
        user with role `user`, store only a bcrypt hash of the password and
        the lowercased email, and return 201 with `UserOut` (no hash).
  FR-2  IF a signup email matches an existing user ignoring case and
        surrounding whitespace, including when two such requests arrive
        concurrently, THE SYSTEM SHALL create at most one user and answer the
        others with 409 `EMAIL_ALREADY_REGISTERED`.
  FR-3  IF a signup or login request has an invalid email, a password
        shorter than 8 characters or longer than 72 bytes (signup), or any
        field not in the request model (including `role`), THE SYSTEM SHALL
        reject it with 422 `VALIDATION_ERROR` and create nothing.
  FR-4  WHEN login credentials are correct, THE SYSTEM SHALL return 200 with
        a signed JWT that expires `JWT_EXPIRY_MINUTES` after issue.
  FR-5  IF the login email is unknown or the password is wrong, THE SYSTEM
        SHALL return the same 401 `INVALID_CREDENTIALS` response in both
        cases and perform one password verification in both cases.
  FR-6  WHEN a request carries a valid, unexpired token whose user still
        exists, THE SYSTEM SHALL treat the request as made by that user with
        the role currently stored in the database.
  FR-7  IF the token is missing, malformed, signed with a different secret,
        expired, uses an algorithm other than HS256, lacks `sub` or `exp`, or
        names a user that no longer exists, THE SYSTEM SHALL answer 401
        `UNAUTHENTICATED`.
  FR-8  IF an authenticated non-admin calls an admin-only route, THE SYSTEM
        SHALL answer 403 `FORBIDDEN`; IF the caller is unauthenticated, THE
        SYSTEM SHALL answer 401, never 403.
  FR-9  WHEN an admin promotes an existing user, THE SYSTEM SHALL set that
        user's role to `admin` (idempotently) so it applies on that user's
        next request with their existing token; IF the target does not
        exist, THE SYSTEM SHALL answer 404 `USER_NOT_FOUND`.
  FR-10 IF `JWT_SECRET` is shorter than 32 characters, THE SYSTEM SHALL
        refuse to start.
  FR-11 THE SYSTEM SHALL hash and verify passwords through the single
        implementation in `src/core/security.py`, used by both the auth
        module and the seed script.
  FR-12 THE SYSTEM SHALL return every error from these endpoints in the
        standard `{"error": {"code", "message"}}` shape.

### 6.1 Requirements Mapping

| Requirement | Design decision / section |
|---|---|
| FR-1 | 2.2 signup, 2.3 email/password rules, `security.hash_password` |
| FR-2 | 3 (insert-then-catch), SPEC-1 `idx_users_email_lower`, test 18 |
| FR-3 | 2.3, request models with `extra="forbid"` |
| FR-4 | 2.4 token lifetime from `JWT_EXPIRY_MINUTES` |
| FR-5 | 2.6 dummy-hash verification, single `INVALID_CREDENTIALS` response |
| FR-6 | 2.4 role not in token, per-request user load (2.5) |
| FR-7 | 2.4 pinned algorithm + required claims, 2.5 |
| FR-8 | 2.5 two dependencies, authentication first |
| FR-9 | 2.2 promote endpoint, 2.4 (role read from DB each request) |
| FR-10 | 2.4 secret length rule in `config.py` |
| FR-11 | 1 (item 5), 4 seed change |
| FR-12 | 2.9 handlers for `ApiError` and request validation errors |

### Equivalence Partitioning + Boundary Value Analysis

Password length (FR-3):
```
Partition 1: < 8 characters           (invalid)   boundary tests: 7 / 8
Partition 2: 8 chars .. 72 bytes      (valid)     boundary tests: 72 bytes / 73 bytes
Partition 3: > 72 bytes               (invalid)
Multi-byte case: 24 Georgian letters = 72 bytes (valid), 25 = 75 bytes (invalid)
```
Email length (FR-3): 254 characters valid, 255 invalid.
Token time (FR-7): expired by 1 second rejected; unexpired accepted.

---

## 7. Logging Requirements

- Expected user-facing failures (duplicate email, bad credentials, expired
  token, 403) are logged at `INFO` with event name and user id when known.
- Admin promotion is logged at `INFO` with the acting admin's id and the
  target user's id (audit trail).
- Unexpected exceptions are logged at `ERROR` with the full traceback.
- Never log passwords, password hashes, tokens, or the `JWT_SECRET`. Email
  addresses are not logged (use user id).

---

## 8. Reuse/Dependency Check

New runtime dependencies introduced by this SPEC (need sign-off with SPEC
approval, per `product.md` boundaries):

- `email-validator` (installed as `pydantic[email]`): strict, maintained email
  syntax checking for Pydantic's `EmailStr`; hand-rolling email validation is
  a known source of bugs. BSD-style/Unlicense-compatible, widely used.

Already declared in `pyproject.toml` and reused: `fastapi`, `uvicorn`,
`pyjwt`, `bcrypt`, `httpx`, `pytest-asyncio`. Nothing else is added.

---

## 9. Non-Goals / Revisit

Out of scope in v1 (each is a deliberate omission, not an oversight):
refresh tokens, logout/token revocation, password reset, email verification,
rate limiting / lockout, demotion of admins, user deletion, CORS (added with
the frontend), OAuth/social login.

Revisit if: the app goes public (add rate limiting and lockout first), or the
frontend needs silent session renewal (refresh tokens).

---

## 10. DRY Check

- Password hashing: one implementation, `src/core/security.py` (FR-11);
  `seed.py`'s private copy is removed.
- `normalize_email`: one definition in `src/core/security.py`, imported by
  `auth` and `seed`.
- Role vocabulary: use `ADMIN_USER_ROLE` / the role constants in
  `src/core/enums.py`; no bare `"admin"` or `"user"` strings in `auth`.
- Error shape: built by one helper in `src/core/errors.py`; later SPECs reuse it.
- Token lifetime: only `JWT_EXPIRY_MINUTES` from settings.

## 11. Single Responsibility Check

- `core/security.py`: crypto and token primitives only (no DB, no HTTP).
- `auth/service.py`: auth business rules over a connection (no HTTP types).
- `auth/dependencies.py`: translating a request into a current user / role
  check (no SQL beyond calling the service).
- `auth/router.py`: parsing and response shaping only.
- `main.py`: app assembly, global error handling, server entry point.

## 11.1 Layer & Dependency Check

Allowed imports: `auth` -> `core`, `db`; `db` -> `core`; `core` -> nothing in
the project. `db` must not import `auth` (this is why `normalize_email` lives
in `core`). Later modules (`movies`, `showtimes`, `reservations`, `reporting`)
depend on `auth.dependencies` only, never on `auth.service` internals.

---

## Amendment Log

(none yet)

---

## Implementation Status

STATUS: NOT STARTED (awaiting approval; see SPEC-2.status)
