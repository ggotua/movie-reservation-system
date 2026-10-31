/**
 * Typed public catalog reads (SPEC-7 Section 2.4, SPEC-3 Section 2.3).
 *
 * The browse page, the day strip and the movie detail page go through these
 * functions; the seat map lives in `seats.ts`.
 */

import { buildQuery, request } from './client'

import type { Movie, Page, Showtime } from './types'

/** Filters accepted by `GET /showtimes`; every one is optional. */
export interface ShowtimeQuery {
  /** A calendar day in `VITE_CINEMA_TIMEZONE`, `YYYY-MM-DD`. */
  date?: string
  /** Only showtimes of this movie. */
  movieId?: number
  /** Page size, 1-100; the API defaults to 20. */
  limit?: number
  /** Rows to skip. */
  offset?: number
}

/**
 * List showtimes (`GET /showtimes`), ordered by start time.
 *
 * @param query - Optional day, movie and pagination filters.
 * @returns One page of showtimes with their movie and screen embedded.
 *
 * @throws ApiError - `VALIDATION_ERROR` (422) for an invalid date or window.
 */
export function listShowtimes(query: ShowtimeQuery = {}): Promise<Page<Showtime>> {
  const search = buildQuery({
    date: query.date,
    movie_id: query.movieId,
    limit: query.limit,
    offset: query.offset,
  })
  return request<Page<Showtime>>(`/showtimes${search}`)
}

/**
 * Read one showtime (`GET /showtimes/{showtime_id}`).
 *
 * @param showtimeId - The showtime's id.
 * @returns The showtime with its movie and screen.
 *
 * @throws ApiError - `SHOWTIME_NOT_FOUND` (404).
 */
export function getShowtime(showtimeId: number): Promise<Showtime> {
  return request<Showtime>(`/showtimes/${showtimeId}`)
}

/**
 * Read one movie (`GET /movies/{movie_id}`).
 *
 * @param movieId - The movie's id.
 * @returns The movie with its genres.
 *
 * @throws ApiError - `MOVIE_NOT_FOUND` (404).
 */
export function getMovie(movieId: number): Promise<Movie> {
  return request<Movie>(`/movies/${movieId}`)
}
