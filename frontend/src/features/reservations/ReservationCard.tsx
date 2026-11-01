/**
 * One reservation drawn as a ticket stub (SPEC-7 FR-7, design brief Section 4).
 *
 * The list view keeps the same stub shape as checkout, one size smaller: the
 * story on the left, a perforated column on the right with the status as a
 * plain word and the single action that fits that status.
 */

import { Link } from 'react-router-dom'

import type { ReservationListItem, ReservationStatus } from '@/api/types'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { formatMoney } from '@/lib/money'
import { formatDay, formatShowtime } from '@/lib/time'

/** Props of {@link ReservationCard}. */
export interface ReservationCardProps {
  /** The reservation, with its showtime embedded. */
  reservation: ReservationListItem
  /** The instant used to decide whether the showtime is still upcoming. */
  now: number
  /** Whether this card's cancel request is in flight. */
  isCancelling: boolean
  /** Sends the cancel request for this reservation. */
  onCancel: () => void
}

/** The status, said as one plain word (design brief Section 4). */
const STATUS_WORDS: Record<ReservationStatus, string> = {
  held: 'Held',
  confirmed: 'Confirmed',
  cancelled: 'Cancelled',
  expired: 'Expired',
}

/** The inline link style shared by the card's actions. */
const LINK_CLASS =
  'text-ink underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink'

/**
 * Write the booked seats the way the stub speaks them, e.g. `B4, B5, B6`.
 *
 * @param reservation - The reservation whose `seats` are written out.
 * @returns The labels joined with a comma and a space.
 */
function seatLabels(reservation: ReservationListItem): string {
  return reservation.seats
    .map((seat) => `${seat.row_label}${seat.seat_number}`)
    .join(', ')
}

/**
 * Render one reservation as a stub.
 *
 * Cancel is offered only for a confirmed booking whose showtime has not
 * started, because the backend rejects any other cancel; a live hold instead
 * links back to its checkout page, which is the only place it can be settled.
 *
 * @param props - The reservation, the current instant and the cancel action.
 * @returns The stub with its status and eligible action.
 */
export function ReservationCard({
  reservation,
  now,
  isCancelling,
  onCancel,
}: ReservationCardProps) {
  const { showtime } = reservation
  const startsAt = Date.parse(showtime.starts_at)
  const isUpcoming = Number.isFinite(startsAt) && startsAt > now
  const canCancel = reservation.status === 'confirmed' && isUpcoming

  return (
    <article className="flex flex-col border border-ink bg-paper sm:flex-row sm:items-stretch">
      <div className="min-w-0 flex-1 p-4">
        <h2 className="text-4">{showtime.movie.title}</h2>
        <p className="mt-1 text-1 text-muted-foreground">
          {formatDay(showtime.starts_at)}, {formatShowtime(showtime.starts_at)}
        </p>
        <p className="text-1 text-muted-foreground">{showtime.screen.name}</p>
        <p className="mt-3 text-2">{seatLabels(reservation)}</p>
        <p className="text-2">{formatMoney(reservation.total_cents)}</p>
      </div>

      <div className="relative flex flex-wrap items-center gap-3 border-t border-dashed border-aisle p-4 sm:w-56 sm:flex-col sm:items-start sm:justify-center sm:border-t-0 sm:border-l">
        <span
          aria-hidden="true"
          className="absolute -top-1.5 -left-1.5 hidden size-3 rounded-full bg-foyer sm:block"
        />
        <span
          aria-hidden="true"
          className="absolute -bottom-1.5 -left-1.5 hidden size-3 rounded-full bg-foyer sm:block"
        />
        <p className="text-2 font-medium">{STATUS_WORDS[reservation.status]}</p>

        {reservation.status === 'held' && (
          <Link to={`/checkout/${reservation.id}`} className={`text-1 ${LINK_CLASS}`}>
            Go to your current hold
          </Link>
        )}

        {canCancel && (
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button
                type="button"
                variant="outline"
                className="h-10"
                disabled={isCancelling}
              >
                Cancel booking
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Cancel this booking?</AlertDialogTitle>
                <AlertDialogDescription>
                  The seats become available to others.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Keep booking</AlertDialogCancel>
                <AlertDialogAction onClick={onCancel}>
                  Cancel booking
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        )}
      </div>
    </article>
  )
}
