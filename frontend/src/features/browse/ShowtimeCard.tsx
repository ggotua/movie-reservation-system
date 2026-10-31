/**
 * One showtime drawn as a ticket stub (SPEC-7 prompt 5, design brief Section 2).
 *
 * The stub is a link to the seat map. Time, screen and price are all real text,
 * so assistive technology reads the whole ticket as the link's name. A showtime
 * that has already started stays linked but is drawn in muted tones.
 */

import { Link } from 'react-router-dom'

import type { Showtime } from '@/api/types'
import { formatMoney } from '@/lib/money'
import { formatShowtime } from '@/lib/time'
import { cn } from '@/lib/utils'

/** Props of {@link ShowtimeCard}. */
export interface ShowtimeCardProps {
  /** The showtime to draw; its embedded movie and screen supply the labels. */
  showtime: Showtime
  /**
   * The instant to compare the start against, in epoch milliseconds. Callers
   * pass the query's `dataUpdatedAt`; reading the clock during render would be
   * impure (React's purity rule) and would render a different stub on every
   * re-render.
   */
  now: number
}

/**
 * Render a showtime as a notched ticket stub.
 *
 * @param props - The showtime.
 * @returns A link to `/showtimes/{id}` with the time in the display face and
 *   the screen name and price beside it.
 *
 * Non-obvious: "past" is decided from the instant against `now`, not from the
 * selected day, so a showtime that started an hour ago is muted while one later
 * today is not. The page background is `foyer`, which is why the perforation
 * notches are painted in that token.
 */
export function ShowtimeCard({ showtime, now }: ShowtimeCardProps) {
  const isPast = new Date(showtime.starts_at).getTime() < now

  return (
    <Link
      to={`/showtimes/${showtime.id}`}
      className={cn(
        'relative inline-flex rounded-stub border bg-paper focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink',
        isPast
          ? 'border-aisle text-muted-foreground'
          : 'border-ink text-ink',
      )}
    >
      <span className="flex items-center px-3 py-2 font-display text-4 font-bold leading-none">
        {formatShowtime(showtime.starts_at)}
      </span>
      <span className="relative flex flex-col justify-center gap-0.5 border-l border-dashed border-aisle py-2 pr-3 pl-3 text-1">
        <span
          aria-hidden="true"
          className="absolute -top-1.5 -left-1.5 size-3 rounded-full bg-foyer"
        />
        <span
          aria-hidden="true"
          className="absolute -bottom-1.5 -left-1.5 size-3 rounded-full bg-foyer"
        />
        <span>{showtime.screen.name}</span>
        <span>{formatMoney(showtime.price_cents)}</span>
      </span>
    </Link>
  )
}
