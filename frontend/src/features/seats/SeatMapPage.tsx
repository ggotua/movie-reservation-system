/**
 * The seat map and hold screen (SPEC-7 FR-3, FR-4, FR-5, FR-12, FR-14; design
 * brief Section 4).
 *
 * The left column holds the showtime header and the seat grid; the ticket
 * summary sits beside it on desktop and becomes a bar at the bottom of a phone.
 * The map is refetched every five seconds while the tab is visible (D6), and a
 * selected seat that stops being `available` is dropped from the selection with
 * a message naming it.
 *
 * Every hold refusal is handled by its code (Section 2.5): a taken seat means
 * "refresh the map and drop the lost seats", an existing hold gets a link to it,
 * and a started showtime disables holding.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'

import { getShowtime } from '@/api/catalog'
import { ApiError } from '@/api/client'
import { holdSeats, listReservations } from '@/api/reservations'
import { getSeatMap } from '@/api/seats'
import type { Seat, SeatMap, Showtime } from '@/api/types'
import { ContentWrapper } from '@/components/layout/content-wrapper'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useAuth } from '@/features/auth/AuthContext'
import { GENERIC_ERROR_MESSAGE, messageFor } from '@/lib/errorMessages'
import { formatMoney } from '@/lib/money'
import { formatDay, formatShowtime } from '@/lib/time'

import { MAX_SEATS, dropUnavailable, toggleSeat } from './seatSelection'
import { SeatGrid } from './SeatGrid'

/** How often the seat map is refetched while the tab is visible (D6). */
const POLL_INTERVAL_MS = 5000

/**
 * Read a route parameter as a showtime id.
 *
 * @param value - `useParams().showtimeId`, possibly `undefined` on a mistyped
 *   URL.
 * @returns The positive integer id, or `null` when the value cannot be one.
 */
function parseShowtimeId(value: string | undefined): number | null {
  if (value === undefined || !/^\d+$/.test(value)) {
    return null
  }
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

/**
 * Format a seat as it is spoken and written, e.g. `B4`.
 *
 * @param seat - The seat to label.
 * @returns The row letter followed by the seat number.
 */
function seatLabel(seat: Seat): string {
  return `${seat.row_label}${seat.seat_number}`
}

/**
 * The sentence shown when seats were dropped from the selection.
 *
 * @param lost - The seats that are no longer available.
 * @returns One sentence naming every lost seat, singular or plural.
 */
function seatLostMessage(lost: readonly Seat[]): string {
  const labels = lost.map((seat) => seatLabel(seat))
  if (labels.length === 1) {
    return `Seat ${labels[0]} was just taken. We removed it from your selection.`
  }
  return `Seats ${labels.join(', ')} were just taken. We removed them from your selection.`
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

/** What the page has to tell the user, if anything. */
type SeatNotice =
  | { kind: 'seats-lost'; seats: Seat[] }
  | { kind: 'message'; text: string }
  | { kind: 'active-hold'; holdId: number | null }

/**
 * Read the notice a previous screen handed over in the route's history state.
 *
 * Checkout sends `Seats released` here after a release (FR-6), so this screen
 * does not repeat that wording: it shows whatever the caller supplied.
 *
 * @param state - `useLocation().state`, i.e. whatever the last navigation set.
 * @returns The notice to show when this screen mounts, or `null` for none.
 */
function noticeFromRouteState(state: unknown): SeatNotice | null {
  if (typeof state !== 'object' || state === null) {
    return null
  }
  const { notice } = state as { notice?: unknown }
  if (typeof notice !== 'string' || notice === '') {
    return null
  }
  return { kind: 'message', text: notice }
}

/** The chosen seats plus the seats a refresh took away, for the message. */
interface SelectionState {
  /** The seats a hold would send; only ever `available` ones. */
  seats: Seat[]
  /** The seats dropped since the user's last action, or `null`. */
  lost: Seat[] | null
}

/**
 * Render the notice above the grid.
 *
 * @param props - The notice to show.
 * @returns A polite status alert: the lost-seat sentence, a message, or the
 *   existing-hold sentence with a link to that hold when its id is known.
 *
 * Non-obvious: `role="status"` replaces the alert's default `alert`, so a
 * polling refresh does not interrupt a screen reader mid-sentence.
 */
function NoticeAlert({ notice }: { notice: SeatNotice }) {
  return (
    <Alert role="status" className="mt-4">
      <AlertDescription>
        {notice.kind === 'message' && notice.text}
        {notice.kind === 'seats-lost' && seatLostMessage(notice.seats)}
        {notice.kind === 'active-hold' && (
          <>
            You already hold seats for this showtime.
            {notice.holdId !== null && (
              <>
                {' '}
                <Link
                  to={`/checkout/${notice.holdId}`}
                  className="underline underline-offset-4"
                >
                  Go to your current hold
                </Link>
                .
              </>
            )}
          </>
        )}
      </AlertDescription>
    </Alert>
  )
}

/**
 * The loading state of the seat map.
 *
 * @returns Five skeleton rows of eight seats, announced as a status, so the
 *   grid keeps its size when the real seats arrive.
 */
function SeatMapSkeleton() {
  return (
    <div role="status" className="mt-6 flex flex-col items-center">
      <span className="sr-only">Loading seats</span>
      <Skeleton className="h-3 w-64 max-w-full" />
      <div className="mt-6 flex flex-col gap-1.5">
        {[0, 1, 2, 3, 4].map((row) => (
          <div key={row} className="flex gap-1.5">
            {[0, 1, 2, 3, 4, 5, 6, 7].map((seat) => (
              <Skeleton
                key={seat}
                className="size-11 rounded-t-[10px] rounded-b-[4px]"
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

/** Props of {@link SeatMapError}. */
interface SeatMapErrorProps {
  /** The friendly message to show. */
  message: string
  /** Refetches the failed data. */
  onRetry: () => void
}

/**
 * Render the error state of the seat map.
 *
 * @param props - The message and the retry action.
 * @returns An alert with a Retry button, so a network failure is never a dead
 *   end (Section 2.5).
 */
function SeatMapError({ message, onRetry }: SeatMapErrorProps) {
  return (
    <Alert className="mt-6">
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
 * Render the page for a URL that cannot be a showtime id.
 *
 * @returns A short message and a link back to the browse page.
 */
function UnknownShowtime() {
  return (
    <main id="main-content" tabIndex={-1} className="py-8">
      <ContentWrapper>
        <h1 className="text-6">Seat map</h1>
        <p className="mt-4 text-2 text-muted-foreground">
          That showtime no longer exists.
        </p>
        <Link
          to="/"
          className="mt-6 inline-block text-ink underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink"
        >
          Browse showtimes
        </Link>
      </ContentWrapper>
    </main>
  )
}

/** Props of {@link TicketSummary}. */
interface TicketSummaryProps {
  /** The showtime whose price and name are shown. */
  showtime: Showtime
  /** The seats the user has chosen. */
  selection: readonly Seat[]
  /** `true` while the hold request is in flight. */
  isHolding: boolean
  /** `true` when the showtime has started, so no hold is possible. */
  isStarted: boolean
  /** Sends the hold request or the login redirect. */
  onHold: () => void
}

/**
 * Render the ticket summary beside the grid, or as a bottom bar on a phone.
 *
 * @param props - The showtime, the selection and the hold action.
 * @returns The chosen seat labels, the seat count, the total and the Hold
 *   button, with the reason it is disabled spelled out in text.
 *
 * Non-obvious: the total is `price_cents` multiplied by the seat count, which
 * stays in integer cents; `formatMoney` performs the single division for
 * display. The authoritative total for a hold is `total_cents` from the server
 * and is shown at checkout.
 */
function TicketSummary({
  showtime,
  selection,
  isHolding,
  isStarted,
  onHold,
}: TicketSummaryProps) {
  const seatCount = selection.length
  const totalCents = showtime.price_cents * seatCount
  const reason = isStarted
    ? 'This showtime has already started.'
    : seatCount === 0
      ? 'Choose at least one seat.'
      : null

  return (
    <div className="lg:sticky lg:top-6">
      <aside
        aria-labelledby="ticket-summary-heading"
        className="fixed inset-x-0 bottom-0 z-20 border-t border-aisle bg-paper px-4 py-3 shadow-[0_-4px_16px_rgba(0,0,0,0.06)] lg:static lg:rounded-control lg:border lg:px-4 lg:py-4 lg:shadow-none"
      >
        <div className="mx-auto flex max-w-xl flex-col gap-2 lg:max-w-none">
          <h2
            id="ticket-summary-heading"
            className="hidden text-1 uppercase tracking-[0.08em] text-muted-foreground lg:block"
          >
            Your seats
          </h2>
          <p className="text-2">
            {seatCount === 0
              ? 'No seats selected.'
              : selection.map((seat) => seatLabel(seat)).join(', ')}
          </p>
          <div className="flex items-baseline justify-between gap-4 border-t border-aisle pt-2">
            <span className="text-1 text-muted-foreground">
              {seatCount} {seatCount === 1 ? 'seat' : 'seats'}
            </span>
            <span className="font-display text-3 font-bold">
              {formatMoney(totalCents)}
            </span>
          </div>
          <Button
            type="button"
            size="lg"
            className="h-11 w-full text-2"
            onClick={onHold}
            disabled={seatCount === 0 || isStarted || isHolding}
            aria-describedby={reason === null ? undefined : 'hold-reason'}
          >
            {isHolding ? 'Holding seats…' : 'Hold seats'}
          </Button>
          {reason !== null && (
            <p id="hold-reason" className="text-1 text-muted-foreground">
              {reason}
            </p>
          )}
        </div>
      </aside>
    </div>
  )
}

/** Props of {@link SeatMapScreen}. */
interface SeatMapScreenProps {
  /** The validated showtime id from the URL. */
  showtimeId: number
}

/**
 * Render the seat map of one showtime and own the selection and hold state.
 *
 * @param props - The showtime id.
 * @returns The header, the grid and the ticket summary, each with its loading
 *   and error state.
 *
 * Non-obvious: the seat map query polls every five seconds but only while the
 * tab is focused (`refetchIntervalInBackground: false`), so a background tab
 * stops creating requests. The "selection lost seats" rule is applied from a
 * subscription to the query cache rather than from an effect that watches the
 * data: the poll, the retry after `SEAT_UNAVAILABLE` and the first load all
 * deliver fresh data through the same `success` event, and only a callback of
 * an external subscription may update state outside an event handler. The "has
 * this showtime started" check compares `starts_at` against the last successful
 * fetch time rather than the wall clock, which keeps the render pure and still
 * refreshes with each poll.
 */
function SeatMapScreen({ showtimeId }: SeatMapScreenProps) {
  const { token } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const showtimeQuery = useQuery({
    queryKey: ['showtimes', showtimeId],
    queryFn: () => getShowtime(showtimeId),
  })
  const seatMapQuery = useQuery({
    queryKey: ['seat-map', showtimeId],
    queryFn: () => getSeatMap(showtimeId),
    refetchInterval: POLL_INTERVAL_MS,
    refetchIntervalInBackground: false,
  })

  const [selectionState, setSelectionState] = useState<SelectionState>({
    seats: [],
    lost: null,
  })
  const [notice, setNotice] = useState<SeatNotice | null>(() =>
    noticeFromRouteState(location.state),
  )
  const [isHolding, setIsHolding] = useState(false)
  const [hasStarted, setHasStarted] = useState(false)
  const queryClient = useQueryClient()

  const selection = selectionState.seats
  const seatMap = seatMapQuery.data
  const showtime = showtimeQuery.data

  useEffect(() => {
    return queryClient.getQueryCache().subscribe((event) => {
      if (event.type !== 'updated' || event.action.type !== 'success') {
        return
      }
      const key = event.query.queryKey
      if (key[0] !== 'seat-map' || key[1] !== showtimeId) {
        return
      }
      const freshMap = event.query.state.data as SeatMap | undefined
      if (freshMap === undefined) {
        return
      }
      setSelectionState((current) => {
        const result = dropUnavailable(current.seats, freshMap)
        if (result.lostSeats.length === 0) {
          return current
        }
        return { seats: result.selection, lost: result.lostSeats }
      })
    })
  }, [queryClient, showtimeId])

  const activeNotice: SeatNotice | null =
    notice ??
    (selectionState.lost === null
      ? null
      : { kind: 'seats-lost', seats: selectionState.lost })

  const startsAt =
    showtime === undefined ? null : new Date(showtime.starts_at).getTime()
  const checkedAt = Math.max(
    showtimeQuery.dataUpdatedAt,
    seatMapQuery.dataUpdatedAt,
  )
  const isStarted =
    hasStarted ||
    (startsAt !== null && Number.isFinite(startsAt) && startsAt <= checkedAt)

  function handleToggle(seat: Seat) {
    const result = toggleSeat(selection, seat, MAX_SEATS)
    if (result.reason === 'too-many-seats') {
      setNotice({ kind: 'message', text: messageFor('TOO_MANY_SEATS') })
      return
    }
    if (result.reason !== null) {
      return
    }
    setSelectionState({ seats: result.selection, lost: null })
    setNotice(null)
  }

  /**
   * Turn a failed hold into the message the user should see.
   *
   * @param error - The thrown value from `holdSeats`.
   */
  async function applyHoldFailure(error: unknown) {
    if (!(error instanceof ApiError)) {
      setNotice({ kind: 'message', text: messageForFailure(error) })
      return
    }

    if (error.code === 'ACTIVE_HOLD_EXISTS') {
      let holdId: number | null
      try {
        const page = await listReservations({ status: 'held', limit: 1 })
        holdId = page.items[0]?.id ?? null
      } catch {
        holdId = null
      }
      setNotice({ kind: 'active-hold', holdId })
      return
    }

    if (error.code === 'SHOWTIME_STARTED') {
      setHasStarted(true)
    }

    if (error.code === 'SEAT_UNAVAILABLE') {
      const refreshed = await seatMapQuery.refetch()
      if (refreshed.data !== undefined) {
        const result = dropUnavailable(selection, refreshed.data)
        setSelectionState({
          seats: result.selection,
          lost: result.lostSeats.length > 0 ? result.lostSeats : null,
        })
        if (result.lostSeats.length > 0) {
          return
        }
      }
    }

    setNotice({ kind: 'message', text: messageFor(error.code, error.message) })
  }

  async function handleHold() {
    if (selection.length === 0 || isHolding) {
      return
    }

    if (token === null) {
      const next = `/showtimes/${showtimeId}`
      navigate(`/login?next=${encodeURIComponent(next)}`)
      return
    }

    setIsHolding(true)
    setNotice(null)
    try {
      const reservation = await holdSeats({
        showtimeId,
        seatIds: selection.map((seat) => seat.seat_id),
      })
      navigate(`/checkout/${reservation.id}`)
    } catch (error) {
      await applyHoldFailure(error)
    } finally {
      setIsHolding(false)
    }
  }

  function retry() {
    void showtimeQuery.refetch()
    void seatMapQuery.refetch()
  }

  const failure = showtimeQuery.isError
    ? showtimeQuery.error
    : seatMapQuery.isError
      ? seatMapQuery.error
      : null

  return (
    <main id="main-content" tabIndex={-1} className="py-8 pb-44 lg:pb-12">
      <ContentWrapper>
        <div className="lg:grid lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-start lg:gap-10">
          <div className="min-w-0">
            {showtime !== undefined && (
              <header>
                <h1 className="text-6 sm:text-7">{showtime.movie.title}</h1>
                <p className="mt-2 text-2 text-muted-foreground">
                  {formatDay(showtime.starts_at)},{' '}
                  {formatShowtime(showtime.starts_at)}
                </p>
                <p className="text-2 text-muted-foreground">
                  {showtime.screen.name}
                </p>
              </header>
            )}

            {activeNotice !== null && <NoticeAlert notice={activeNotice} />}

            <section aria-label="Seat map">
              {showtimeQuery.isPending || seatMapQuery.isPending ? (
                <SeatMapSkeleton />
              ) : null}

              {failure !== null && (
                <SeatMapError
                  message={messageForFailure(failure)}
                  onRetry={retry}
                />
              )}

              {seatMap !== undefined && showtime !== undefined && (
                <div className="mt-6">
                  <SeatGrid
                    seatMap={seatMap}
                    selection={selection}
                    onToggle={handleToggle}
                  />
                </div>
              )}
            </section>
          </div>

          {showtime !== undefined && (
            <TicketSummary
              showtime={showtime}
              selection={selection}
              isHolding={isHolding}
              isStarted={isStarted}
              onHold={() => {
                void handleHold()
              }}
            />
          )}
        </div>
      </ContentWrapper>
    </main>
  )
}

/**
 * Render the seat map page for the `:showtimeId` route parameter.
 *
 * @returns The seat map, or a short message when the URL cannot be a showtime
 *   id.
 */
export function SeatMapPage() {
  const { showtimeId } = useParams<{ showtimeId: string }>()
  const id = parseShowtimeId(showtimeId)

  if (id === null) {
    return <UnknownShowtime />
  }

  return <SeatMapScreen showtimeId={id} />
}
