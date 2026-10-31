/**
 * TanStack Query hooks for the public catalog (SPEC-7 Section 2.4, FR-2).
 *
 * Every screen reads data through these hooks, never through `fetch`; the
 * functions in `api/catalog.ts` stay the only network code.
 */

import { useQuery } from '@tanstack/react-query'

import { getMovie, listShowtimes } from '@/api/catalog'
import { ApiError } from '@/api/client'
import { messageFor } from '@/lib/errorMessages'

/** The browse page asks for a whole day in one page (SPEC-7 Section 2.4). */
const DAY_PAGE_LIMIT = 100

/**
 * Load one day's showtimes for the browse page.
 *
 * @param day - The calendar day in `YYYY-MM-DD`, interpreted by the backend in
 *   the cinema time zone.
 * @returns The query result for `GET /showtimes?date=&limit=100`. Changing the
 *   day changes the query key, so the list refetches automatically.
 */
export function useDayShowtimes(day: string) {
  return useQuery({
    queryKey: ['showtimes', 'day', day],
    queryFn: () => listShowtimes({ date: day, limit: DAY_PAGE_LIMIT }),
  })
}

/**
 * Load a movie's upcoming showtimes for the movie page.
 *
 * @param movieId - The movie's id.
 * @returns The query result for `GET /showtimes?movie_id=`. The backend already
 *   limits this list to showtimes that have not started.
 */
export function useMovieShowtimes(movieId: number) {
  return useQuery({
    queryKey: ['showtimes', 'movie', movieId],
    queryFn: () => listShowtimes({ movieId }),
  })
}

/**
 * Load one movie, including its genres and description.
 *
 * @param movieId - The movie's id.
 * @returns The query result for `GET /movies/{movie_id}`.
 */
export function useMovieDetails(movieId: number) {
  return useQuery({
    queryKey: ['movies', movieId],
    queryFn: () => getMovie(movieId),
  })
}

/**
 * Turn a caught query failure into the message to show.
 *
 * @param error - Whatever the query's `error` holds.
 * @returns The friendly text for the backend code (SPEC-7 FR-8), or the
 *   generic message when the failure is not an `ApiError`.
 */
export function messageForLoadError(error: unknown): string {
  if (error instanceof ApiError) {
    return messageFor(error.code, error.message)
  }
  return messageFor(null)
}
