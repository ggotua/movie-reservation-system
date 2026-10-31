/**
 * Friendly messages for every backend error code (SPEC-7 FR-8, D4).
 *
 * Backend errors carry a stable `SCREAMING_SNAKE_CASE` code and a message that
 * is safe to show. The UI branches on the code and shows the text below, so no
 * code, JSON or stack trace ever reaches the user. The map is deliberately in
 * one file so a Georgian translation can replace it later.
 */

/** Shown when a code is unknown and the server sent no usable message. */
export const GENERIC_ERROR_MESSAGE = 'Something went wrong. Try again.'

/**
 * Every backend error code this app can receive, mapped to user-facing text.
 *
 * Covers SPEC-2 (auth), SPEC-3 (catalog), SPEC-4 (seats and holds), SPEC-5
 * (list and cancel), SPEC-6 (reporting), the shared `VALIDATION_ERROR` /
 * `INTERNAL_ERROR` handlers, Starlette's `NOT_FOUND`, `METHOD_NOT_ALLOWED` and
 * `HTTP_ERROR`, and the client-only `NETWORK_ERROR`. Test 11 scans
 * `backend/src` and fails when a new backend code is missing here.
 */
export const ERROR_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  // Client-side failures (api/client.ts).
  NETWORK_ERROR: "Can't reach the server. Check your connection and try again.",

  // SPEC-2: auth and roles.
  UNAUTHENTICATED: 'Your session has ended. Log in to continue.',
  FORBIDDEN: "You don't have permission to do that.",
  INVALID_CREDENTIALS: 'Incorrect email or password.',
  EMAIL_ALREADY_REGISTERED:
    'An account with this email already exists. Log in instead.',
  USER_NOT_FOUND: 'No account with that id exists.',

  // SPEC-3: genres, movies, screens and showtimes.
  GENRE_NOT_FOUND: 'That genre no longer exists.',
  MOVIE_NOT_FOUND: 'That movie is no longer available.',
  GENRE_ALREADY_EXISTS: 'A genre with that name already exists.',
  GENRE_IN_USE: 'This genre is attached to one or more movies and cannot be deleted.',
  MOVIE_HAS_SHOWTIMES: 'This movie has showtimes and cannot be deleted.',
  SCREEN_NOT_FOUND: 'That screen no longer exists.',
  SCREEN_NAME_TAKEN: 'A screen with that name already exists.',
  SHOWTIME_NOT_FOUND: 'That showtime no longer exists.',
  SHOWTIME_IN_PAST: 'A showtime must start in the future.',
  SHOWTIME_OVERLAP: 'Another showtime on this screen is too close to that start time.',
  SHOWTIME_STARTED: 'This showtime has already started.',
  SHOWTIME_HAS_RESERVATIONS: 'This showtime has bookings and cannot be deleted.',

  // SPEC-4 and SPEC-5: seat maps, holds, confirm, release, list and cancel.
  SEAT_NOT_FOUND: 'One or more of those seats no longer exist. Refresh the seat map.',
  SEAT_UNAVAILABLE:
    'Someone else just took one of those seats. Refresh the seat map and pick again.',
  ACTIVE_HOLD_EXISTS:
    'You already hold seats for this showtime. Go to your current hold.',
  TOO_MANY_SEATS: 'You can hold at most 8 seats at a time.',
  RESERVATION_NOT_FOUND: "That booking doesn't exist.",
  RESERVATION_EXPIRED: 'This hold has expired. Choose seats again.',
  RESERVATION_NOT_HELD: 'This booking is not an active hold.',
  RESERVATION_NOT_CONFIRMED: 'Only confirmed bookings can be cancelled.',

  // SPEC-6: admin reports.
  INVALID_DATE_RANGE: 'Pick a valid date range and try again.',

  // Shared handlers in backend/src/main.py.
  VALIDATION_ERROR: "Some of the details you entered aren't valid. Check them and try again.",
  INTERNAL_ERROR: 'Something went wrong on our side. Try again.',
  NOT_FOUND: "The page or item you asked for doesn't exist.",
  METHOD_NOT_ALLOWED: "That action isn't available.",
  HTTP_ERROR: "The request couldn't be completed. Try again.",
})

/**
 * Resolve the message to show for an error code.
 *
 * @param code - The backend's `error.code`; `null`/`undefined` when the body
 *   could not be read.
 * @param serverMessage - The backend's `error.message`, if any. Only used for
 *   an unknown code: the friendly text is preferred for a known one.
 * @returns The mapped friendly message; for an unknown code the generic
 *   sentence plus the server's message, or the generic sentence alone when the
 *   server sent none.
 */
export function messageFor(
  code: string | null | undefined,
  serverMessage?: string | null,
): string {
  const known = typeof code === 'string' ? ERROR_MESSAGES[code] : undefined
  if (known !== undefined) {
    return known
  }
  const extra = typeof serverMessage === 'string' ? serverMessage.trim() : ''
  return extra === '' ? GENERIC_ERROR_MESSAGE : `${GENERIC_ERROR_MESSAGE} ${extra}`
}
