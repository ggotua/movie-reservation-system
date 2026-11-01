/**
 * The reservations list (SPEC-7 FR-7, FR-8, FR-14; design brief Section 4).
 *
 * The caller's bookings, newest first, each drawn as a small ticket stub. Two
 * plain selects filter by status and by time; the page keeps its place with
 * Previous and Next buttons built from the envelope's `limit`, `offset` and
 * `total`.
 *
 * Cancel lives here and only on a confirmed booking whose showtime has not
 * started, because that is the only cancel the backend accepts. A live hold
 * links to its checkout page instead.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import type { ChangeEvent } from 'react'

import { ApiError } from '@/api/client'
import { cancelReservation, listReservations } from '@/api/reservations'
import type { ReservationListItem, ReservationStatus } from '@/api/types'
import { ContentWrapper } from '@/components/layout/content-wrapper'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { GENERIC_ERROR_MESSAGE, messageFor } from '@/lib/errorMessages'

import { ReservationCard } from './ReservationCard'

/** Rows asked for per page; the API accepts 1-100. */
const PAGE_SIZE = 10

/** The select's class list, sized for a 40 px touch target. */
const SELECT_CLASS =
  'h-10 rounded-control border border-aisle bg-paper px-2 text-2 text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

/** A status filter value; `all` sends no `status` parameter. */
type StatusFilter = 'all' | ReservationStatus

/** A time filter value; `all` sends no `upcoming` parameter. */
type TimeFilter = 'all' | 'upcoming' | 'past'

/** A short line telling the user how the last cancel went. */
interface PageNotice {
  /** `error` is drawn in the destructive colour. */
  kind: 'message' | 'error'
  /** The text to show. */
  text: string
}

/** Props of {@link ReservationsSkeleton}. */
interface ReservationsSkeletonProps {
  /** How many stub-shaped placeholders to draw. */
  rows: number
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
 * Narrow a select value to a {@link StatusFilter}.
 *
 * @param value - The `value` of the changed `<select>`.
 * @returns `true` when the value is one this page offers.
 */
function isStatusFilter(value: string): value is StatusFilter {
  return (
    value === 'all' ||
    value === 'held' ||
    value === 'confirmed' ||
    value === 'cancelled' ||
    value === 'expired'
  )
}

/**
 * Narrow a select value to a {@link TimeFilter}.
 *
 * @param value - The `value` of the changed `<select>`.
 * @returns `true` when the value is one this page offers.
 */
function isTimeFilter(value: string): value is TimeFilter {
  return value === 'all' || value === 'upcoming' || value === 'past'
}

/**
 * The loading state of the list.
 *
 * @param props - How many placeholders to draw.
 * @returns Stub-shaped skeletons, so the page does not jump when rows arrive.
 */
function ReservationsSkeleton({ rows }: ReservationsSkeletonProps) {
  return (
    <div
      className="mt-6 flex flex-col gap-4"
      role="status"
      aria-label="Loading reservations"
    >
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-32 w-full" />
      ))}
    </div>
  )
}

/**
 * Render the caller's reservations with filters, pagination and cancel.
 *
 * @returns The list, or its loading, empty and error states.
 */
export function MyReservationsPage() {
  const [status, setStatus] = useState<StatusFilter>('all')
  const [time, setTime] = useState<TimeFilter>('all')
  const [offset, setOffset] = useState(0)
  const [notice, setNotice] = useState<PageNotice | null>(null)
  const [cancellingId, setCancellingId] = useState<number | null>(null)

  const reservationsQuery = useQuery({
    queryKey: ['reservations', 'list', status, time, offset],
    queryFn: () =>
      listReservations({
        status: status === 'all' ? undefined : status,
        upcoming: time === 'all' ? undefined : time === 'upcoming',
        limit: PAGE_SIZE,
        offset,
      }),
  })
  const page = reservationsQuery.data
  const items = page?.items ?? []
  const total = page?.total ?? 0
  const now = reservationsQuery.dataUpdatedAt
  const hasPrevious = offset > 0
  const hasNext = offset + PAGE_SIZE < total

  function handleStatusChange(event: ChangeEvent<HTMLSelectElement>) {
    const value = event.target.value
    if (!isStatusFilter(value)) {
      return
    }
    setStatus(value)
    setOffset(0)
    setNotice(null)
  }

  function handleTimeChange(event: ChangeEvent<HTMLSelectElement>) {
    const value = event.target.value
    if (!isTimeFilter(value)) {
      return
    }
    setTime(value)
    setOffset(0)
    setNotice(null)
  }

  function handleOffset(next: number) {
    setOffset(Math.max(0, next))
    setNotice(null)
  }

  async function handleCancel(reservation: ReservationListItem) {
    if (cancellingId !== null) {
      return
    }
    setCancellingId(reservation.id)
    setNotice(null)
    try {
      await cancelReservation(reservation.id)
      setNotice({ kind: 'message', text: 'Booking cancelled' })
      await reservationsQuery.refetch()
    } catch (error) {
      setNotice({ kind: 'error', text: messageForFailure(error) })
    } finally {
      setCancellingId(null)
    }
  }

  return (
    <main id="main-content" tabIndex={-1} className="py-8">
      <ContentWrapper>
        <h1 className="text-6 sm:text-7">My reservations</h1>

        <div className="mt-6 flex flex-wrap gap-4">
          <div className="flex flex-col gap-1">
            <Label htmlFor="reservation-status">Status</Label>
            <select
              id="reservation-status"
              className={SELECT_CLASS}
              value={status}
              onChange={handleStatusChange}
            >
              <option value="all">All</option>
              <option value="held">Held</option>
              <option value="confirmed">Confirmed</option>
              <option value="cancelled">Cancelled</option>
              <option value="expired">Expired</option>
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <Label htmlFor="reservation-time">Time</Label>
            <select
              id="reservation-time"
              className={SELECT_CLASS}
              value={time}
              onChange={handleTimeChange}
            >
              <option value="all">All</option>
              <option value="upcoming">Upcoming</option>
              <option value="past">Past</option>
            </select>
          </div>
        </div>

        {notice !== null &&
          (notice.kind === 'error' ? (
            <Alert variant="destructive" className="mt-6">
              <AlertDescription>{notice.text}</AlertDescription>
            </Alert>
          ) : (
            <Alert className="mt-6">
              <AlertDescription>{notice.text}</AlertDescription>
            </Alert>
          ))}

        {reservationsQuery.isPending && <ReservationsSkeleton rows={3} />}

        {reservationsQuery.isError && (
          <Alert className="mt-6">
            <AlertDescription className="flex flex-col items-start gap-3">
              <p>{messageForFailure(reservationsQuery.error)}</p>
              <Button
                type="button"
                variant="outline"
                className="h-10 px-4"
                onClick={() => {
                  void reservationsQuery.refetch()
                }}
              >
                Try again
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {page !== undefined && items.length === 0 && (
          <p className="mt-8 text-2 text-muted-foreground">No bookings to show.</p>
        )}

        {items.length > 0 && (
          <ul
            aria-label="Reservations"
            className="mt-6 flex flex-col gap-4"
          >
            {items.map((reservation) => (
              <li key={reservation.id}>
                <ReservationCard
                  reservation={reservation}
                  now={now}
                  isCancelling={cancellingId === reservation.id}
                  onCancel={() => {
                    void handleCancel(reservation)
                  }}
                />
              </li>
            ))}
          </ul>
        )}

        {(hasPrevious || hasNext) && (
          <div className="mt-6 flex items-center justify-between gap-4">
            <Button
              type="button"
              variant="outline"
              className="h-10 px-4"
              disabled={!hasPrevious}
              onClick={() => handleOffset(offset - PAGE_SIZE)}
            >
              Previous
            </Button>
            <p className="text-1 text-muted-foreground">
              {items.length} of {total}
            </p>
            <Button
              type="button"
              variant="outline"
              className="h-10 px-4"
              disabled={!hasNext}
              onClick={() => handleOffset(offset + PAGE_SIZE)}
            >
              Next
            </Button>
          </div>
        )}
      </ContentWrapper>
    </main>
  )
}
