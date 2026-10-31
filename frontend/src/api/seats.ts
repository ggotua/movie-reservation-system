/**
 * The typed seat-map read (SPEC-7 Section 2.4, SPEC-4 Section 2.3).
 *
 * The map is fetched fresh on every visit and on every retry after a conflict;
 * there is no client-side cache, because another user's hold can change any
 * seat between two requests (SPEC-4 D6).
 */

import { request } from './client'

import type { SeatMap } from './types'

/**
 * Read the seat map of one showtime (`GET /showtimes/{showtime_id}/seats`).
 *
 * @param showtimeId - The showtime whose seats to read.
 * @returns Every seat with its derived `state` (`available`, `held`, `taken`).
 *
 * @throws ApiError - `SHOWTIME_NOT_FOUND` (404) for an unknown showtime.
 */
export function getSeatMap(showtimeId: number): Promise<SeatMap> {
  return request<SeatMap>(`/showtimes/${showtimeId}/seats`)
}
