/**
 * Client-side grouping of one page of showtimes (SPEC-7 prompt 5).
 *
 * `GET /showtimes` returns a flat list ordered by start time. The browse page
 * wants one block per movie and the movie page wants one block per day, so the
 * two functions here do exactly that, without touching the data.
 */

import type { MovieSummary, Showtime } from '@/api/types'
import { dayInCinema } from '@/lib/time'

/** One movie and all of its showtimes from the current page, in start order. */
export interface MovieShowtimeGroup {
  /** The movie embedded in every showtime of this group. */
  movie: MovieSummary
  /** The movie's showtimes, as the API ordered them. */
  showtimes: Showtime[]
}

/**
 * Group showtimes by movie, keeping the API's order.
 *
 * @param showtimes - The flat page of showtimes (`items` from the response).
 * @returns One group per movie, ordered by the movie's first showtime. A movie
 *   with no showtimes cannot appear, because the movie comes from a showtime.
 */
export function groupShowtimesByMovie(
  showtimes: readonly Showtime[],
): MovieShowtimeGroup[] {
  const groups = new Map<number, MovieShowtimeGroup>()
  for (const showtime of showtimes) {
    const group = groups.get(showtime.movie.id)
    if (group === undefined) {
      groups.set(showtime.movie.id, {
        movie: showtime.movie,
        showtimes: [showtime],
      })
    } else {
      group.showtimes.push(showtime)
    }
  }
  return [...groups.values()]
}

/** One calendar day and its showtimes. */
export interface DayShowtimeGroup {
  /** The day in `YYYY-MM-DD`, in the cinema time zone. */
  day: string
  /** The day's showtimes, as the API ordered them. */
  showtimes: Showtime[]
}

/**
 * Group showtimes by their calendar day in the cinema time zone.
 *
 * @param showtimes - The flat page of showtimes (`items` from the response).
 * @returns One group per day, ordered by the day's first showtime. The key uses
 *   `dayInCinema`, so a showtime shortly after midnight in Tbilisi lands under
 *   its local day even when its UTC date is the day before.
 */
export function groupShowtimesByDay(
  showtimes: readonly Showtime[],
): DayShowtimeGroup[] {
  const groups = new Map<string, DayShowtimeGroup>()
  for (const showtime of showtimes) {
    const day = dayInCinema(showtime.starts_at)
    const group = groups.get(day)
    if (group === undefined) {
      groups.set(day, { day, showtimes: [showtime] })
    } else {
      group.showtimes.push(showtime)
    }
  }
  return [...groups.values()]
}
