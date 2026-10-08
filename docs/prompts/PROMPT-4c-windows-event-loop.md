# PROMPT 4c: Windows event loop fix for the seed script

Found when running `python -m src.db.seed` against Neon on Windows:
`psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in
async mode`. Python on Windows defaults to ProactorEventLoop; async psycopg
needs SelectorEventLoop. Alembic is unaffected (sync). Fix applied in
`backend/src/db/seed.py` only: the entry point runs
`asyncio.run(run_seed(), loop_factory=asyncio.SelectorEventLoop)` when
`sys.platform == "win32"`.

Verified: seed run twice without error, counts `(2, 152, 1)` on Neon dev DB.

## Open item for SPEC-2 (API layer)

uvicorn on Windows has the same constraint with async psycopg. SPEC-2 (or
the app entry point) must select SelectorEventLoop on Windows before the
server starts. Not needed on Linux/CI.

```
@workspace
Fix the seed script so it runs on Windows. Read backend/src/db/seed.py first.
(IMPLEMENTER RULES as in PROMPT 1.)

Where the script entry point calls asyncio.run(run_seed()), use
asyncio.run(run_seed(), loop_factory=asyncio.SelectorEventLoop) when
sys.platform == "win32", and plain asyncio.run(run_seed()) otherwise.
Add a short comment explaining why. Change nothing else.
Then run ruff format src, ruff check src, mypy --strict src, and report.
Do NOT run the seed script.
```
