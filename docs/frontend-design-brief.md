# Frontend Design Brief — Movie Reservation (user app)

Applies to SPEC-7. Visual and copy decisions only; behavior is defined in the SPEC.
The brief is the design input for the Cursor prompts (PROMPT-7).

## 1. Subject, audience, job

- **Subject:** a single cinema's booking site (screens, showtimes, numbered seats).
- **Audience:** ordinary moviegoers on phones and laptops, often deciding in a
  hurry; English UI now, Georgian later.
- **Primary job:** pick a showtime and seats quickly, with confidence that the
  seats are really theirs. The seat map is the product; everything else leads to it.

## 2. Concept: "box office"

The vernacular of the subject is the paper ticket and the auditorium: stubs with
perforated edges, row letters, velvet and brass, house lights going down. The
design uses these as *structure*, not decoration:

- a reservation is a **ticket stub** (notched edge, a perforation line, the
  hold countdown running along it);
- showtimes are **rows of stubs** under each movie, not a grid of identical cards;
- the **seat map is the one memorable thing**; everything around it is quiet.

## 3. Tokens

**Color (light, "foyer")**

| Name | Hex | Role |
|---|---|---|
| foyer | #EDF0F3 | page background |
| paper | #FBFCFD | surfaces, tickets, seat (available) |
| ink | #1D2733 | text, outlines |
| velvet | #9B1D34 | primary actions, selected seats |
| brass | #A87A1F | held seats (fill only, never text) |
| aisle | #B7BFC9 | taken seats, disabled, dividers |

**Color (dark, "house lights down"; follows the system setting)**

| Name | Hex | Role |
|---|---|---|
| foyer | #17131C | page background |
| paper | #221C29 | surfaces, tickets |
| ink | #ECE8F0 | text |
| velvet | #D4506B | primary actions, selected seats |
| brass | #D9A441 | held seats |
| aisle | #4A4455 | taken seats, disabled, dividers |

Contrast: ink on foyer/paper and white on velvet are at least 4.5:1; brass is a
fill with ink-colored marks on it, never text on paper.

**Type (two families, clearly different)**

- Display: **Big Shoulders Display** (condensed, marquee-sign feeling), weights
  700 and 800. Used for movie titles, showtime times, page titles, row letters.
- Body: **IBM Plex Sans**, weights 400, 500, 600. Used for everything else.
- Self-hosted through Fontsource packages (no external font requests). Add a
  Georgian-capable fallback (system sans) in the font stack for the future.
- Scale: 14 / 16 / 18 / 22 / 28 / 40 / 56 px; body 16 px with line-height 1.5;
  line length at most 65 characters; sentence case everywhere.

**Shape (hierarchy, not one radius for everything)**

- Seats: armchair shape, 10 px radius on top corners, 3 px at the bottom.
- Buttons and inputs: 8 px.
- Ticket stubs: square corners with semicircular notches at the perforation.
- Posters: 4 px.
- No gradient washes, no soft grey drop shadows; separation by tone and
  1 px lines in `aisle`.

**Motion**

- Only motion that answers an action: a seat press (80 ms scale and check mark),
  the ticket summary updating, the hold countdown moving along the perforation.
- No entrance animations, no hover transitions on every card.
- `prefers-reduced-motion`: all of the above become instant.

## 4. Layout

**Browse (`/`)** — the page title *is* the date, set large in the display face
("Saturday 31 October"). Above it, a horizontal day strip (today and the next 13
days, scrollable, today selected). Below it, one block per movie: poster
(small, left), title in display type, genres in plain text, then that movie's
showtimes as ticket stubs in a row (time large, price small, screen name small).

```
Saturday 31 October
[ Fri 30 ][ SAT 31 ][ Sun 1 ][ Mon 2 ] ...            <- day strip, left aligned
-------------------------------------------------------
[poster]  The Long Quiet                  genres
          [ 19:30 | Screen 1 | 12.00 GEL ] [ 21:45 | ... ]
-------------------------------------------------------
[poster]  Harbour Lights
          [ 18:00 | Screen 2 | 10.00 GEL ]
```

**Seat map (`/showtimes/:id`)** — left aligned content column on desktop,
ticket summary on the right (sticky); on a phone the summary is a bar at the
bottom. The screen is drawn as a gently curved line above the rows with the word
"Screen" under it. Row letters sit on both sides in display type and stay visible
while the grid scrolls sideways. A visual aisle gap after half the seats of a row
(layout only; seat numbers stay continuous).

```
 The Long Quiet            Sat 31 Oct, 19:30
 Screen 1                  +--------------------+
        ___________        | Your seats         |
       (  Screen   )       |  B4  B5  B6        |
 A  [] [] [] [] [] [] A    | 3 seats   36.00GEL |
 B  [] [] [x] [x] [] [] B  | [  Hold seats  ]   |
 C  ...                    +--------------------+
 legend: available | selected | held | taken
```

Seat states never rely on color alone:

| State | Look |
|---|---|
| available | paper fill, 1.5 px ink outline |
| selected | velvet fill, white check mark |
| held (by someone) | brass fill with diagonal hatching |
| taken | aisle fill with an X mark |

Seats are real buttons, at least 40 px square with 6 px gaps; focus shows a 2 px
ink ring with a velvet outline.

**Checkout (`/checkout/:id`)** — one large ticket stub centered in a narrow
column: movie, time, screen, seats, total. The countdown is a thin line along the
perforation that shortens, with the remaining time in text next to it. Two
actions: "Confirm booking" (primary, velvet) and "Release seats" (plain).

**My reservations (`/reservations`)** — a list of smaller stubs, newest first;
status as a plain word on the stub ("Confirmed", "Cancelled", "Expired", "Held");
filters as two simple selects above the list.

## 5. Copy

Plain verbs, sentence case, active voice, the same name for an action in the
button and in the result.

| Where | Text |
|---|---|
| Hold button | Hold seats |
| After hold | Seats held for 10:00 (live countdown) |
| Confirm | Confirm booking, then "Booking confirmed" |
| Release | Release seats, then "Seats released" |
| Cancel | Cancel booking (asks "Cancel this booking? The seats become available to others."), then "Booking cancelled" |
| Empty day | No showtimes on this day. Pick another date. |
| Empty reservations | You have no bookings yet. Pick a showtime to get started. |
| Seat lost | Seat B4 was just taken. We removed it from your selection. |
| Active hold | You already hold seats for this showtime. Go to your current hold. |
| Expired | This hold has expired. Choose seats again. |
| Network error | Can't reach the server. Check your connection and try again. |

Errors say what happened and what to do next; they never apologize and never show
codes or JSON.

## 6. Avoid (defaults that would make this look generic)

No uppercase tracked-out labels above headings; no strings joined with middle dots;
no arrows appended to buttons; no numbered markers; no single accented word in a
headline; no identical rounded cards in a grid; no gradient decoration; no cream +
serif + terracotta look; no near-black + neon accent.

## 7. Quality floor and self-check

Responsive from 360 px wide, visible keyboard focus everywhere, reduced motion
respected, both color schemes readable, touch targets at least 40 px for seats and
buttons. Before finishing the UI work, remove one decoration that does not help a
person book seats.
