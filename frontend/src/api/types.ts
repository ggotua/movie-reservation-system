/**
 * TypeScript mirror of the backend wire formats (SPEC-7 Section 2.2, D5).
 *
 * Every name is the backend schema's field name verbatim, including
 * `snake_case`, so a response can be used without a mapping layer and a rename
 * on the server shows up as a compile error here. These are types only: no
 * validation, no defaults, no logic.
 */

/** `users.role` (SPEC-2). New accounts are always `user`. */
export type UserRole = 'user' | 'admin'

/** `UserOut` (SPEC-2 Section 2.2). */
export interface User {
  id: number
  email: string
  role: UserRole
  created_at: string
}

/** `TokenOut` (SPEC-2 Section 2.2), the body of `POST /auth/login`. */
export interface Token {
  access_token: string
  token_type: 'bearer'
  expires_in: number
}

/** `GenreOut` and `GenreRef` (SPEC-3 Section 2.3), which share one shape. */
export interface Genre {
  id: number
  name: string
}

/** `MovieOut` (SPEC-3 Section 2.3). */
export interface Movie {
  id: number
  title: string
  description: string
  poster_url: string | null
  genres: Genre[]
  created_at: string
  updated_at: string
}

/** `MovieRef` (SPEC-3 Section 2.3), the movie embedded in a showtime. */
export interface MovieSummary {
  id: number
  title: string
  poster_url: string | null
}

/** `ScreenRef` (SPEC-3 Section 2.3), the screen embedded in a showtime. */
export interface ScreenSummary {
  id: number
  name: string
}

/** `ShowtimeOut` (SPEC-3 Section 2.3). */
export interface Showtime {
  id: number
  starts_at: string
  price_cents: number
  movie: MovieSummary
  screen: ScreenSummary
}

/** `SeatType` (`core/enums.py`). */
export type SeatType = 'standard' | 'accessible'

/** `SeatState` (`reservations/rules.py`), derived per request. */
export type SeatState = 'available' | 'held' | 'taken'

/** `SeatOut` (SPEC-4 Section 2.3): one seat of the public seat map. */
export interface Seat {
  seat_id: number
  row_label: string
  seat_number: number
  seat_type: SeatType
  state: SeatState
}

/** `SeatMapOut`'s nested screen (SPEC-4 Section 2.3), with seat dimensions. */
export interface SeatMapScreen {
  id: number
  name: string
  rows: number
  seats_per_row: number
}

/** `SeatMapOut` (SPEC-4 Section 2.3). */
export interface SeatMap {
  showtime_id: number
  screen: SeatMapScreen
  seats: Seat[]
}

/** `ReservationStatus` (`core/enums.py`); `status` is the effective status. */
export type ReservationStatus = 'held' | 'confirmed' | 'cancelled' | 'expired'

/** `SeatRef` (SPEC-4 Section 2.3), a seat recorded on a reservation. */
export interface ReservationSeat {
  seat_id: number
  row_label: string
  seat_number: number
}

/** `ReservationOut` (SPEC-4 Section 2.3). */
export interface Reservation {
  id: number
  showtime_id: number
  status: ReservationStatus
  seats: ReservationSeat[]
  total_cents: number
  created_at: string
  expires_at: string | null
  confirmed_at: string | null
  cancelled_at: string | null
}

/** `ShowtimeSummary` (SPEC-5 Section 2.3), embedded in a list item. */
export interface ReservationShowtime {
  id: number
  starts_at: string
  price_cents: number
  movie: MovieSummary
  screen: ScreenSummary
}

/** `ReservationListItem` (SPEC-5 Section 2.3): `ReservationOut` plus showtime. */
export interface ReservationListItem extends Reservation {
  showtime: ReservationShowtime
}

/** `Page[T]` (SPEC-3 FR-14), the shared list envelope. */
export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

/** The `error` object every API error body carries (`core/errors.py`). */
export interface ApiErrorDetail {
  code: string
  message: string
}

/** The standard error body: `{"error": {"code": ..., "message": ...}}`. */
export interface ApiErrorBody {
  error: ApiErrorDetail
}
