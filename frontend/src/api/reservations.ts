/**
 * Typed reservation endpoints (SPEC-7 Section 2.4, SPEC-4 and SPEC-5).
 *
 * The hold window, ownership checks and every status transition are the
 * server's rules; these functions only send the request and return the
 * reservation the server reports back.
 */

import { buildQuery, request } from './client'

import type {
  Page,
  Reservation,
  ReservationListItem,
  ReservationStatus,
} from './types'

/** The body of `POST /reservations`. */
export interface HoldSeatsInput {
  showtimeId: number
  seatIds: number[]
}

/** Filters accepted by `GET /reservations`; every one is optional. */
export interface ReservationListQuery {
  /** Filter on the effective status. */
  status?: ReservationStatus
  /** `true` keeps future showtimes, `false` keeps started ones. */
  upcoming?: boolean
  /** Page size, 1-100; the API defaults to 20. */
  limit?: number
  /** Rows to skip. */
  offset?: number
}

/**
 * Hold seats (`POST /reservations`).
 *
 * @param input - The showtime and the distinct seat ids to hold.
 * @returns The new `held` reservation, with `expires_at` and `total_cents`.
 *
 * @throws ApiError - `SEAT_UNAVAILABLE` (409), `SEAT_NOT_FOUND` (404),
 *   `SHOWTIME_STARTED` (409), `ACTIVE_HOLD_EXISTS` (409) or `TOO_MANY_SEATS`
 *   (422).
 */
export function holdSeats(input: HoldSeatsInput): Promise<Reservation> {
  const body = { showtime_id: input.showtimeId, seat_ids: input.seatIds }
  return request<Reservation>('/reservations', { method: 'POST', body })
}

/**
 * List the caller's reservations (`GET /reservations`).
 *
 * @param query - Optional status, upcoming and pagination filters.
 * @returns One page of reservations, each with its showtime embedded.
 *
 * @throws ApiError - `VALIDATION_ERROR` (422) for an invalid status or window.
 */
export function listReservations(
  query: ReservationListQuery = {},
): Promise<Page<ReservationListItem>> {
  const search = buildQuery({
    status: query.status,
    upcoming: query.upcoming,
    limit: query.limit,
    offset: query.offset,
  })
  return request<Page<ReservationListItem>>(`/reservations${search}`)
}

/**
 * Read one reservation (`GET /reservations/{reservation_id}`).
 *
 * @param reservationId - The reservation's id.
 * @returns The reservation with its effective status.
 *
 * @throws ApiError - `RESERVATION_NOT_FOUND` (404) for someone else's booking.
 */
export function getReservation(reservationId: number): Promise<Reservation> {
  return request<Reservation>(`/reservations/${reservationId}`)
}

/**
 * Confirm a held reservation (`POST /reservations/{id}/confirm`).
 *
 * @param reservationId - The reservation's id.
 * @returns The `confirmed` reservation.
 *
 * @throws ApiError - `RESERVATION_EXPIRED` (409) or `RESERVATION_NOT_HELD`
 *   (409).
 */
export function confirmReservation(reservationId: number): Promise<Reservation> {
  return request<Reservation>(`/reservations/${reservationId}/confirm`, {
    method: 'POST',
  })
}

/**
 * Release a hold (`POST /reservations/{id}/release`).
 *
 * @param reservationId - The reservation's id.
 * @returns The `cancelled` reservation.
 *
 * @throws ApiError - `RESERVATION_NOT_HELD` (409) when it is not an active hold.
 */
export function releaseReservation(reservationId: number): Promise<Reservation> {
  return request<Reservation>(`/reservations/${reservationId}/release`, {
    method: 'POST',
  })
}

/**
 * Cancel a confirmed booking (`POST /reservations/{id}/cancel`).
 *
 * @param reservationId - The reservation's id.
 * @returns The `cancelled` reservation.
 *
 * @throws ApiError - `RESERVATION_NOT_CONFIRMED` (409).
 */
export function cancelReservation(reservationId: number): Promise<Reservation> {
  return request<Reservation>(`/reservations/${reservationId}/cancel`, {
    method: 'POST',
  })
}
