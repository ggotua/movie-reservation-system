/**
 * The checkout screen (SPEC-7 FR-6, FR-8, FR-14; design brief Section 4).
 *
 * One reservation drawn as a large ticket stub in a narrow centered column:
 * movie, local time, screen, seats and the server's `total_cents`. A `held`
 * reservation also gets the live countdown, drawn as text next to a thin line
 * along the perforation that shortens as the hold runs out.
 *
 * The client clock is display only. When the countdown reaches zero the
 * reservation is refetched once and the SERVER status decides what to show, so
 * a wrong or changed client clock can never keep a hold alive.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { getShowtime } from '@/api/catalog'
import { ApiError } from '@/api/client'
import {
  confirmReservation,
  getReservation,
  releaseReservation,
} from '@/api/reservations'
import type { Reservation, ReservationSeat } from '@/api/types'
import { ContentWrapper } from '@/components/layout/content-wrapper'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { formatRemaining, isExpired, remainingMs } from '@/lib/countdown'
import { GENERIC_ERROR_MESSAGE, messageFor } from '@/lib/errorMessages'
import { formatMoney } from '@/lib/money'
import { formatDay, formatShowtime } from '@/lib/time'

/** How often the hold countdown is redrawn while a hold is live. */
const TICK_INTERVAL_MS = 1000

/** The inline link style used by the stub's follow-up actions. */
const LINK_CLASS =
  'text-ink underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink'

/** Which action is in flight, so both buttons stay disabled together. */
type PendingAction = 'confirm' | 'release'

/** Props of {@link CheckoutFailure}. */
interface CheckoutFailureProps {
  /** The friendly message for the failed call. */
  message: string
  /** Refetches the reservation and the showtime. */
  onRetry: () => void
}

/** Props of {@link CheckoutScreen}. */
interface CheckoutScreenProps {
  /** The reservation id taken from the route. */
  reservationId: number
}

/**
 * Read a route parameter as a reservation id.
 *
 * @param value - `useParams().reservationId`, possibly `undefined` on a
 *   mistyped URL.
 * @returns The positive integer id, or `null` when the value cannot be one.
 */
function parseReservationId(value: string | undefined): number | null {
  if (value === undefined || !/^\d+$/.test(value)) {
    return null
  }
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

/**
 * Write the held seats the way the stub speaks them, e.g. `B4, B5, B6`.
 *
 * @param seats - The `seats` of a reservation, in row then number order.
 * @returns The labels joined with a comma and a space.
 */
function seatLabels(seats: readonly ReservationSeat[]): string {
  return seats.map((seat) => `${seat.row_label}${seat.seat_number}`).join(', ')
}

/**
 * The message for a failed call.
 *
 * @param error - The thrown value from an `api/` function.
 * @returns The friendly text for the backend code, or the generic sentence for
 *   anything that is not an {@link ApiError}.
 */
function messageForFailure(error: unknown): string {
  return error instanceof ApiError
    ? messageFor(error.code, error.message)
    : GENERIC_ERROR_MESSAGE
}

/**
 * Share of the hold window that is left, for the countdown line.
 *
 * @param reservation - The `held` reservation; `expires_at` must be set.
 * @param remaining - Milliseconds left, already clamped at zero.
 * @returns A number between 0 and 1; `0` when the window cannot be measured,
 *   which draws an empty line rather than an over-long one.
 */
function remainingShare(reservation: Reservation, remaining: number): number {
  if (reservation.expires_at === null) {
    return 0
  }
  const total =
    Date.parse(reservation.expires_at) - Date.parse(reservation.created_at)
  if (!Number.isFinite(total) || total <= 0) {
    return 0
  }
  return Math.min(1, Math.max(0, remaining / total))
}

/**
 * The loading state of the checkout screen.
 *
 * @returns A stub-shaped skeleton, so the column keeps its size when the
 *   reservation arrives.
 */
function CheckoutSkeleton() {
  return (
    <div
      className="mx-auto mt-8 max-w-md"
      role="status"
      aria-label="Loading booking"
    >
      <Skeleton className="h-64 w-full" />
    </div>
  )
}

/**
 * Render a failed load with a retry action (FR-14).
 *
 * @param props - The message and the retry action.
 * @returns An alert that never leaves the user at a dead end.
 */
function CheckoutFailure({ message, onRetry }: CheckoutFailureProps) {
  return (
    <Alert className="mx-auto mt-8 max-w-md">
      <AlertDescription className="flex flex-col items-start gap-3">
        <p>{message}</p>
        <Button type="button" variant="outline" size="lg" onClick={onRetry}>
          Try again
        </Button>
      </AlertDescription>
    </Alert>
  )
}

/**
 * The two links offered once a booking is settled.
 *
 * @returns Links to the reservations list and to browsing.
 */
function SettledLinks() {
  return (
    <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-2">
      <Link to="/reservations" className={LINK_CLASS}>
        My reservations
      </Link>
      <Link to="/" className={LINK_CLASS}>
        Browse
      </Link>
    </div>
  )
}

/**
 * Render the ticket stub and the actions that belong to its status.
 *
 * @param props - The route's reservation id.
 * @returns The stub, the countdown while a hold is live, and the follow-up
 *   actions for the server's status.
 */
function CheckoutScreen({ reservationId }: CheckoutScreenProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const reservationQuery = useQuery({
    queryKey: ['reservations', reservationId],
    queryFn: () => getReservation(reservationId),
  })
  const reservation = reservationQuery.data
  const showtimeId = reservation?.showtime_id
  const showtimeQuery = useQuery({
    queryKey: ['showtimes', showtimeId],
    queryFn: () => {
      if (showtimeId === undefined) {
        throw new Error('The reservation has no showtime')
      }
      return getShowtime(showtimeId)
    },
    enabled: showtimeId !== undefined,
  })
  const showtime = showtimeQuery.data

  const [now, setNow] = useState(() => Date.now())
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const refetchedFor = useRef<string | null>(null)

  const isHeld = reservation?.status === 'held'
  const expiresAt = isHeld ? reservation.expires_at : null
  const remaining = expiresAt === null ? 0 : remainingMs(expiresAt, now)
  const hasRunOut = expiresAt !== null && isExpired(expiresAt, now)
  const { refetch: refetchReservation } = reservationQuery

  useEffect(() => {
    if (!isHeld) {
      return
    }
    const timer = window.setInterval(() => setNow(Date.now()), TICK_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [isHeld])

  // A hold that has run out is not decided here: one refetch asks the server,
  // and the status it reports is what the screen renders.
  useEffect(() => {
    if (expiresAt === null || !hasRunOut || refetchedFor.current === expiresAt) {
      return
    }
    refetchedFor.current = expiresAt
    void refetchReservation()
  }, [expiresAt, hasRunOut, refetchReservation])

  async function handleConfirm(target: Reservation) {
    if (pendingAction !== null) {
      return
    }
    setPendingAction('confirm')
    setActionError(null)
    try {
      const confirmed = await confirmReservation(target.id)
      queryClient.setQueryData(['reservations', target.id], confirmed)
    } catch (error) {
      setActionError(messageForFailure(error))
      void refetchReservation()
    } finally {
      setPendingAction(null)
    }
  }

  async function handleRelease(target: Reservation) {
    if (pendingAction !== null) {
      return
    }
    setPendingAction('release')
    setActionError(null)
    try {
      await releaseReservation(target.id)
      navigate(`/showtimes/${target.showtime_id}`, {
        state: { notice: 'Seats released' },
      })
    } catch (error) {
      setActionError(messageForFailure(error))
      void refetchReservation()
    } finally {
      setPendingAction(null)
    }
  }

  function retry() {
    void reservationQuery.refetch()
    void showtimeQuery.refetch()
  }

  const failure = reservationQuery.isError
    ? reservationQuery.error
    : showtimeQuery.isError
      ? showtimeQuery.error
      : null

  return (
    <>
      {reservationQuery.isPending && <CheckoutSkeleton />}

      {failure !== null && (
        <CheckoutFailure message={messageForFailure(failure)} onRetry={retry} />
      )}

      {reservation !== undefined && showtime !== undefined && (
        <div className="mx-auto mt-8 max-w-md">
          <article className="border border-ink bg-paper">
            <div className="p-4 sm:p-6">
              <h2 className="text-5">{showtime.movie.title}</h2>
              <p className="mt-2 text-2 text-muted-foreground">
                {formatDay(showtime.starts_at)},{' '}
                {formatShowtime(showtime.starts_at)}
              </p>
              <p className="text-2 text-muted-foreground">
                {showtime.screen.name}
              </p>

              <dl className="mt-6 flex flex-col gap-2 border-t border-aisle pt-4">
                <div className="flex items-baseline justify-between gap-4">
                  <dt className="text-1 text-muted-foreground">Seats</dt>
                  <dd className="text-right text-2">
                    {seatLabels(reservation.seats)}
                  </dd>
                </div>
                <div className="flex items-baseline justify-between gap-4">
                  <dt className="text-1 text-muted-foreground">Total</dt>
                  <dd className="font-display text-3 font-bold">
                    {formatMoney(reservation.total_cents)}
                  </dd>
                </div>
              </dl>
            </div>

            {expiresAt !== null && (
              <div className="relative flex items-center gap-3 border-t border-dashed border-aisle px-4 py-3 sm:px-6">
                <span
                  aria-hidden="true"
                  className="absolute -top-1.5 -left-1.5 size-3 rounded-full bg-foyer"
                />
                <span
                  aria-hidden="true"
                  className="absolute -bottom-1.5 -left-1.5 size-3 rounded-full bg-foyer"
                />
                <span
                  aria-hidden="true"
                  className="absolute -top-1.5 -right-1.5 size-3 rounded-full bg-foyer"
                />
                <span
                  aria-hidden="true"
                  className="absolute -right-1.5 -bottom-1.5 size-3 rounded-full bg-foyer"
                />
                <span aria-hidden="true" className="h-0.5 flex-1 bg-aisle">
                  <span
                    className="block h-full bg-velvet"
                    style={{
                      width: `${remainingShare(reservation, remaining) * 100}%`,
                    }}
                  />
                </span>
                <p className="text-1 text-muted-foreground">
                  Seats held for {formatRemaining(remaining)}
                </p>
              </div>
            )}
          </article>

          {actionError !== null && (
            <Alert variant="destructive" className="mt-6">
              <AlertDescription>{actionError}</AlertDescription>
            </Alert>
          )}

          {reservation.status === 'held' && (
            <div className="mt-6 flex flex-col gap-2 sm:flex-row">
              <Button
                type="button"
                size="lg"
                className="h-11 flex-1 text-2"
                aria-busy={pendingAction === 'confirm'}
                disabled={pendingAction !== null}
                onClick={() => {
                  void handleConfirm(reservation)
                }}
              >
                Confirm booking
              </Button>
              <Button
                type="button"
                size="lg"
                variant="outline"
                className="h-11 flex-1 text-2"
                aria-busy={pendingAction === 'release'}
                disabled={pendingAction !== null}
                onClick={() => {
                  void handleRelease(reservation)
                }}
              >
                Release seats
              </Button>
            </div>
          )}

          {reservation.status === 'confirmed' && (
            <div className="mt-6">
              <p className="text-3">Booking confirmed</p>
              <SettledLinks />
            </div>
          )}

          {reservation.status === 'expired' && (
            <div className="mt-6">
              <p className="text-2">{messageFor('RESERVATION_EXPIRED')}</p>
              <p className="mt-4 text-2">
                <Link to={`/showtimes/${showtime.id}`} className={LINK_CLASS}>
                  Choose seats again
                </Link>
              </p>
            </div>
          )}

          {reservation.status === 'cancelled' && (
            <div className="mt-6">
              <p className="text-3">Booking cancelled</p>
              <SettledLinks />
            </div>
          )}

        </div>
      )}
    </>
  )
}

/**
 * Render the page for a URL that cannot be a reservation id.
 *
 * @returns A short message and a link back to the browse page.
 */
function UnknownReservation() {
  return (
    <div className="mx-auto mt-8 max-w-md">
      <p className="text-2 text-muted-foreground">
        {messageFor('RESERVATION_NOT_FOUND')}
      </p>
      <p className="mt-4 text-2">
        <Link to="/" className={LINK_CLASS}>
          Browse showtimes
        </Link>
      </p>
    </div>
  )
}

/**
 * Render the checkout page for the `:reservationId` route parameter.
 *
 * @returns The ticket stub, or a short message when the URL cannot be a
 *   reservation id.
 */
export function CheckoutPage() {
  const { reservationId } = useParams<{ reservationId: string }>()
  const id = parseReservationId(reservationId)

  return (
    <main id="main-content" tabIndex={-1} className="py-8">
      <ContentWrapper>
        <h1 className="text-6 sm:text-7">Checkout</h1>
        {id === null ? (
          <UnknownReservation />
        ) : (
          <CheckoutScreen reservationId={id} />
        )}
      </ContentWrapper>
    </main>
  )
}
