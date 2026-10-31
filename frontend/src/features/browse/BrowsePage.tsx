/**
 * The browse screen (SPEC-7 FR-2, FR-10, FR-12, FR-14; design brief Section 4).
 *
 * The page title is the selected date, the day strip below it offers today and
 * the next thirteen days, and one block per movie lists that movie's showtimes
 * as ticket stubs. The selected day lives in the URL (`?date=YYYY-MM-DD`), so a
 * reload or a shared link opens the same day; an absent or invalid value falls
 * back to today in the cinema time zone.
 */

import { Link, useSearchParams } from 'react-router-dom'

import { Skeleton } from '@/components/ui/skeleton'
import { dayStrip, formatDay, todayInCinema } from '@/lib/time'

import { DayStrip } from './DayStrip'
import { LoadError } from './LoadError'
import { MoviePoster } from './MoviePoster'
import {
  messageForLoadError,
  useDayShowtimes,
} from './queries'
import { groupShowtimesByMovie } from './showtimeGroups'
import { ShowtimeCard } from './ShowtimeCard'

/** How many days the strip offers, today included. */
const STRIP_DAYS = 14

/** Matches a calendar day in `YYYY-MM-DD` form. */
const DAY_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/

/**
 * Decide whether a URL value is a real calendar day.
 *
 * @param value - The raw `date` search parameter, possibly `null`.
 * @returns `true` only for a well-formed day that exists, so `2026-02-31` is
 *   rejected rather than silently rolled over by `Date`.
 */
function isCalendarDay(value: string | null): value is string {
  if (value === null) {
    return false
  }
  const match = DAY_PATTERN.exec(value)
  if (match === null) {
    return false
  }
  const year = Number(match[1] ?? Number.NaN)
  const month = Number(match[2] ?? Number.NaN)
  const day = Number(match[3] ?? Number.NaN)
  const date = new Date(Date.UTC(year, month - 1, day))
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  )
}

/**
 * The loading state of the movie blocks.
 *
 * @returns Skeleton blocks announced as a status, with the movie count left
 *   deliberately unknown.
 */
function BrowseSkeleton() {
  return (
    <div
      role="status"
      className="flex flex-col gap-6 border-t border-aisle pt-6"
    >
      <span className="sr-only">Loading showtimes</span>
      {[0, 1, 2].map((index) => (
        <div key={index} className="flex gap-4">
          <Skeleton className="aspect-[2/3] w-20 rounded-poster sm:w-28" />
          <div className="flex flex-1 flex-col gap-3">
            <Skeleton className="h-7 w-52" />
            <Skeleton className="h-14 w-full max-w-md" />
          </div>
        </div>
      ))}
    </div>
  )
}

/**
 * Render the browse page.
 *
 * @returns The selected day's title, the day strip and the movie blocks, each
 *   with its loading, empty or error state.
 */
export function BrowsePage() {
  const [searchParams] = useSearchParams()
  const today = todayInCinema()
  const days = dayStrip(today, STRIP_DAYS)
  const requestedDay = searchParams.get('date')
  const selectedDay = isCalendarDay(requestedDay) ? requestedDay : today
  const showtimes = useDayShowtimes(selectedDay)
  const groups = groupShowtimesByMovie(showtimes.data?.items ?? [])

  return (
    <main
      id="main-content"
      tabIndex={-1}
      className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6"
    >
      <h1 className="text-6 sm:text-7">{formatDay(selectedDay)}</h1>

      <div className="mt-6">
        <DayStrip days={days} selectedDay={selectedDay} />
      </div>

      <section aria-labelledby="browse-showtimes" className="mt-8">
        <h2 id="browse-showtimes" className="sr-only">
          Showtimes
        </h2>

        {showtimes.isPending && <BrowseSkeleton />}

        {showtimes.isError && (
          <LoadError
            message={messageForLoadError(showtimes.error)}
            onRetry={() => void showtimes.refetch()}
          />
        )}

        {showtimes.isSuccess && groups.length === 0 && (
          <p className="border-t border-aisle pt-6 text-2 text-muted-foreground">
            No showtimes on this day. Pick another date.
          </p>
        )}

        {groups.length > 0 && (
          <div className="divide-y divide-aisle border-t border-aisle">
            {groups.map(({ movie, showtimes: movieShowtimes }) => (
              <section
                key={movie.id}
                aria-labelledby={`movie-${movie.id}`}
                className="flex gap-4 py-6"
              >
                <MoviePoster
                  src={movie.poster_url}
                  title={movie.title}
                  className="w-20 sm:w-28"
                />
                <div className="flex min-w-0 flex-col gap-3">
                  <h3 id={`movie-${movie.id}`} className="text-4 sm:text-5">
                    <Link
                      to={`/movies/${movie.id}`}
                      className="hover:underline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink"
                    >
                      {movie.title}
                    </Link>
                  </h3>
                  <ul className="flex flex-wrap gap-3">
                    {movieShowtimes.map((showtime) => (
                      <li key={showtime.id}>
                        <ShowtimeCard
                          showtime={showtime}
                          now={showtimes.dataUpdatedAt}
                        />
                      </li>
                    ))}
                  </ul>
                </div>
              </section>
            ))}
          </div>
        )}
      </section>
    </main>
  )
}
