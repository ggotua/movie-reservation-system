# Feature Implementation Plan

## Priority 1: Foundation

### SPEC-1: Data Model & Relationships
Description:   All tables, constraints, indexes, cascade rules (users,
               movies, genres, screens, seats, showtimes, reservations,
               seat_reservations)
Dependencies:  None
Complexity:    Medium
Notes:         The seat-uniqueness constraint designed here is what
               SPEC-4 depends on for correctness — gets extra scrutiny.

### SPEC-2: Auth & Roles
Description:   FastAPI app skeleton (app factory, DB connection dependency,
               standard error shape), signup, login, JWT issuance,
               `get_current_user` / `require_admin` dependencies, admin
               promotion endpoint, shared password hashing (seed script
               reuses it)
Dependencies:  SPEC-1 (users table)
Complexity:    Medium

## Priority 2: Catalog & Scheduling

### SPEC-3: Movie & Showtime Management
Description:   Admin CRUD for movies (+ genres), screens/seats seeding,
               showtime creation; public browsing by date
Dependencies:  SPEC-1, SPEC-2
Complexity:    Medium

## Priority 3: Core Business Logic

### SPEC-4: Seat Reservation Engine
Description:   Hold seats (race-safe), lazy expiry of stale holds, confirm
               (stubbed payment), the concurrency test proving no
               double-booking under simultaneous requests
Dependencies:  SPEC-1, SPEC-2, SPEC-3
Complexity:    High
Notes:         This is the spec the whole project exists to practice —
               gets the most design scrutiny and the Math/Concurrency
               verification gate.

## Priority 4: User-Facing Lifecycle

### SPEC-5: Reservation Lifecycle for Users
Description:   List own reservations, cancel (only upcoming, only owner)
Dependencies:  SPEC-4
Complexity:    Low

## Priority 5: Admin Reporting

### SPEC-6: Admin Reporting
Description:   All reservations (filterable), capacity per showtime,
               revenue by movie/date range — aggregation queries over
               confirmed reservations only
Dependencies:  SPEC-4, SPEC-5
Complexity:    Medium

---

Build order follows this list top to bottom. Each SPEC gets its own
`SPEC-N-*.md` + `SPEC-N.status` gate file per the SDD-V2 workflow before
any implementation prompts are written.
