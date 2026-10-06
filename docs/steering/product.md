# Product — Movie Reservation System

## What this is

A backend system for reserving movie tickets: users sign up, browse movies
and showtimes, reserve specific seats, and manage their own reservations.
Admins manage the movie catalog, showtimes, and see reporting on
reservations, capacity, and revenue.

This is a learning/portfolio project (per the brief) whose real goal is
practicing complex business logic — seat-level concurrency control,
relational data modeling, and reporting queries — not shipping a commercial
product.

## Who it's for

- **Regular users**: browse movies/showtimes by date, reserve seats, view
  and cancel their own upcoming reservations.
- **Admins**: manage movies, showtimes, promote users to admin, view all
  reservations/capacity/revenue.

## What this product is NOT

- Not a payment processor. Reservation "confirmation" is stubbed —no real
  money moves, no PCI scope, no third-party payment gateway integration.
- Not multi-tenant / multi-cinema-chain. One theater's worth of screens and
  showtimes for the scope of this build (a `theater`/`screen` concept exists
  in the data model for realism, but multi-location management, per-theater
  admin scoping, etc. are out of scope).
- Not a notification system. No email/SMS confirmations or reminders.
- Not a public marketing site — no search-engine-facing movie pages, no CMS
  for posters/trailers beyond storing an image URL.
- Not mobile-native. This is a backend/API only; no frontend is in scope
  beyond what's needed to exercise the API (e.g. OpenAPI docs, maybe a thin
  test client).

## Non-Goals (v1)

- No real payment integration (stubbed confirm step only)
- No multi-theater / multi-location support
- No email or SMS notifications
- No seat-map visual editor for admins (seats are defined via structured
  input — rows/columns/type — not a drag-and-drop UI)
- No waitlisting for sold-out showtimes
- No refunds/partial cancellation (a reservation is cancelled in full)
- No dynamic pricing (price is per-showtime, not per-seat-tier, in v1)

## Boundaries

✅ Always — implementer does these without asking:
- Run tests before every commit
- Follow naming/error-shape conventions in `conventions.md`
- Use integer cents (never float) for any money value
- Enforce seat-uniqueness-per-showtime at the database level, not just in
  application code

⚠️ Ask first — pause and confirm before proceeding:
- Any database schema change after a SPEC is approved and implemented
- Adding a new third-party dependency not already named in `tech-stack.md`
- Changing an existing API endpoint's request/response shape
- Changing the seat-hold/confirm expiry window

🚫 Never — hard stop, no exceptions:
- Commit secrets, API keys, or `.env` contents
- Remove a failing test without explicit approval
- Allow two active reservations to hold the same seat for the same showtime
