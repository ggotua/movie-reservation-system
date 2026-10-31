/**
 * The browse page's horizontal day strip (SPEC-7 FR-2, FR-12).
 *
 * Fourteen days are links, so the strip is keyboard operable without extra key
 * handling and works with browser scrolling. The chosen day is marked with
 * `aria-current="date"`, a heavier border and a heavier weight, so the state is
 * not carried by color alone.
 */

import { Link } from 'react-router-dom'

import { formatDayShort } from '@/lib/time'
import { cn } from '@/lib/utils'

/** Props of {@link DayStrip}. */
export interface DayStripProps {
  /** The days to show, `YYYY-MM-DD`, starting with today. */
  days: readonly string[]
  /** The day currently being browsed; may lie outside `days` when the URL says so. */
  selectedDay: string
}

/**
 * Render the day strip.
 *
 * @param props - The days to offer and the selected day.
 * @returns A labelled navigation landmark with one link per day.
 */
export function DayStrip({ days, selectedDay }: DayStripProps) {
  return (
    <nav aria-label="Choose a day" className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
      <ul className="flex w-max gap-2">
        {days.map((day) => {
          const isSelected = day === selectedDay
          return (
            <li key={day}>
              <Link
                to={`/?date=${day}`}
                aria-current={isSelected ? 'date' : undefined}
                className={cn(
                  'flex h-10 min-w-16 items-center justify-center rounded-control border-2 px-3 text-1 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink',
                  isSelected
                    ? 'border-ink bg-paper font-semibold uppercase text-ink'
                    : 'border-aisle text-muted-foreground hover:text-ink',
                )}
              >
                {formatDayShort(day)}
              </Link>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}
