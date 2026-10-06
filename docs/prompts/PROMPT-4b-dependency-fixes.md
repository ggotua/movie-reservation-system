# PROMPT 4b: Dependency fixes and seed hardening

Found while reviewing PROMPT 4 output. Run in Cursor Composer after
PROMPT 4, before PROMPT 5. Steering change already made first:
`docs/steering/tech-stack.md` (passlib replaced by direct bcrypt;
`sqlalchemy[asyncio]`).

Why:
- `sqlalchemy>=2.0` does not install `greenlet`, so any async DB code
  fails with ImportError. Needs `sqlalchemy[asyncio]`.
- `passlib` 1.7.4 (last release 2020) cannot work with `bcrypt` 5.x
  (backend self-test raises "password cannot be longer than 72 bytes").
  Pinning bcrypt below 4.1 would freeze an old library for an unmaintained
  wrapper, so passlib is dropped and bcrypt is used directly.
- The admin insert is keyed on `ON CONFLICT (email)`, which covers only the
  case-sensitive `uq_users_email`. Re-running the seed with the same email
  in a different letter case would hit the case-insensitive
  `idx_users_email_lower` and crash with an IntegrityError instead of being
  idempotent.
- `"admin"` is a bare string literal in seed.py; the role vocabulary must be
  defined once in `src/core/enums.py` (SPEC-1 Section 9).

```
@workspace
Apply the following corrections. Reference: docs/steering/tech-stack.md
(already updated) and docs/prompts/PROMPT-4b-dependency-fixes.md.

IMPLEMENTER RULES — follow without exception:
1. Return COMPLETE files — no "# ... rest unchanged" or partial output.
2. Functional style; no classes except what pydantic-settings requires.
3. Change ONLY what is listed below. No extra features.
4. Do not assume any file/import/variable not in the workspace.
5. Code Documentation Standard: docstrings and type hints on anything you
   touch; "why" comments for non-obvious choices.
6. Never put a secret or real credential in code, comments or .env.example
   (placeholders only).

Changes (exactly these):

A. backend/pyproject.toml
   - "sqlalchemy>=2.0" -> "sqlalchemy[asyncio]>=2.0"
   - remove "passlib[bcrypt]"
   - add "bcrypt>=5.0,<6"
   - remove any mypy/ruff override that existed only for passlib
     (e.g. ignore_missing_imports for passlib), if present.

B. backend/src/core/enums.py
   - add ADMIN_USER_ROLE: UserRole = "admin" next to DEFAULT_USER_ROLE,
     with a one-line comment.

C. backend/src/db/seed.py
   - replace the passlib CryptContext with direct bcrypt:
       bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
     in a small pure function `_hash_password(password: str) -> str`.
   - remove the "# type: ignore[import-untyped]" for passlib.
   - keep the TEMPORARY DUPLICATION comment, updated: when SPEC-2 lands,
     hashing moves to src/core/ and is imported by both places.
   - import ADMIN_USER_ROLE from src.core.enums instead of the "admin"
     literal; remove the _ADMIN_ROLE constant and its justification comment.
   - admin insert: call .on_conflict_do_nothing() WITHOUT index_elements, so
     a conflict on either uniqueness rule (case-sensitive email constraint
     or case-insensitive lower(email) index) is skipped. Add a "why" comment.
     Do not change the screens/seats inserts.

D. backend/.env.example
   - add SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD with obvious placeholder
     values (e.g. admin@example.com / change-me-before-use), and a comment
     that they are only read by `python -m src.db.seed`.

After editing, from backend/ with the venv active, run and report REAL
output (no summaries):
   pip install -e .
   pip check
   ruff format src
   ruff check src
   mypy src --strict
   python -c "import bcrypt; h = bcrypt.hashpw(b'x', bcrypt.gensalt()); print(bcrypt.checkpw(b'x', h))"
   python -m src.db.seed        (with SEED_* unset: must fail loudly, exit code 1)

Return the complete changed files.
```

## After running it

In your own terminal (venv active), clean the local workarounds from
PROMPT 4 so the venv matches pyproject.toml:

```powershell
cd "D:\Web Development\reservation system\backend"
pip uninstall -y passlib
pip install -e .
pip check
```

Then commit:

```powershell
cd "D:\Web Development\reservation system"
git add backend docs
git commit -m "feat(db): Seed script; fix async/bcrypt dependencies [SPEC-1, PROMPT-4, PROMPT-4b]"
```
