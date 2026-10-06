# PROMPT 1b: Patch tables.py for SPEC-1 Amendment 1

Run in Cursor Composer AFTER PROMPT 2 and BEFORE PROMPT 3 (migration).
Requires the amended `docs/specs/SPEC-1-database-schema.md` in the
workspace (see its "Amendment Log"), and SPEC-1.status set to `approved`
again by you.

```
@workspace
Patch backend/src/db/tables.py to match SPEC-1 Amendment 1.
Reference: docs/specs/SPEC-1-database-schema.md — Sections 2.8, 2.9, FR-7
and the Amendment Log (read these first).

IMPLEMENTER RULES — follow without exception:
1. Return the COMPLETE file — no "# ... rest unchanged" or partial output.
2. Functional style; Table objects only, no ORM classes.
3. Change ONLY what is listed below. Every other table, column, constraint,
   index and name stays exactly as it is now. No extra features.
4. Do not assume any file, import or variable not in the workspace. If
   something is missing, say so.
5. Docstrings/type hints/"why" comments per the project standard; update
   the module docstring or comments only where they describe the changed
   constraints.
6. Before writing code, restate FR-7 in one sentence. If anything is
   ambiguous, ask before implementing.

Changes (exactly these):

A. Table `reservations`: add
   UniqueConstraint("id", "showtime_id", name="reservations_id_showtime_unique").
   Add a comment: redundant with the primary key on purpose; it exists only
   as the target of the composite foreign key on seat_reservations.

B. Table `seat_reservations`:
   - Remove the ForeignKey("reservations.id", ondelete="CASCADE") from the
     `reservation_id` column (the column itself stays: BigInteger,
     nullable=False).
   - Add a table-level
     ForeignKeyConstraint(
         ["reservation_id", "showtime_id"],
         ["reservations.id", "reservations.showtime_id"],
         ondelete="CASCADE",
         name="seat_reservations_reservation_showtime_fk",
     )
   - KEEP the existing single-column ForeignKey on `showtime_id` ->
     showtimes.id (ondelete="RESTRICT") and on `seat_id` -> seats.id
     (ondelete="RESTRICT"), unchanged.
   - Import ForeignKeyConstraint from sqlalchemy.

C. Do NOT change the partial unique index
   idx_seat_reservations_active_unique or any CHECK constraint.

After editing, re-run (and report the real output of) from backend/:
   ruff format src
   ruff check src
   mypy src --strict
and a temporary offline DDL compile check (delete the script afterward)
that asserts:
   - the CREATE TABLE for reservations contains
     CONSTRAINT reservations_id_showtime_unique UNIQUE (id, showtime_id)
   - the CREATE TABLE for seat_reservations contains
     CONSTRAINT seat_reservations_reservation_showtime_fk
       FOREIGN KEY(reservation_id, showtime_id)
       REFERENCES reservations (id, showtime_id) ON DELETE CASCADE
   - seat_reservations still has exactly two other FKs (showtimes, seats),
     both ON DELETE RESTRICT
   - the partial unique index DDL is unchanged
   - CHECK constraint names are unchanged

Return the complete file.
```

## After running it

```powershell
cd backend
ruff check src
mypy src --strict
git add .
git commit -m "feat(db): Add enums and table definitions incl. composite FK [SPEC-1 Amendment 1, PROMPT-2, PROMPT-1b]"
```

Then continue with PROMPT 3 (migration). The migration must contain the
unique constraint and the composite FK; PROMPT 5's tests now include test
13 (FR-7).
