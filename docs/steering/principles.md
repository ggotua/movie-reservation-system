# Principles — Movie Reservation System

Non-negotiable rules. One line each. If a SPEC or implementation conflicts
with one of these, the SPEC is wrong until this file is deliberately
changed — not the other way around.

- All money is integer cents, never float.
- No two active (held or confirmed) reservations may occupy the same seat
  for the same showtime — enforced by a database constraint, not just
  application-layer checking.
- Business rules never live inside a FastAPI route handler or an
  SQLAlchemy `Table`/migration — they live in a module function that can
  be called and tested without the framework or a live request.
- Authentication (who is this?) and authorization (may they do this, to
  this specific resource?) are always two separate, explicit checks. A
  route is never "protected" by login alone.
- A seat hold has a fixed expiry; nothing holds a seat indefinitely.
- If uncertain whether something is in scope, it is not — check
  `product.md`'s Non-Goals before building it.
- No direct database access from the `reporting` module's aggregation
  logic without going through the same table definitions as every other
  module — one schema, one source of truth for what a "confirmed
  reservation" is.
