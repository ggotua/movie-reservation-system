/**
 * The movie page (SPEC-7 FR-2, FR-10, FR-14; design brief Section 4).
 *
 * Shows the movie's title, description, genres and poster, then its upcoming
 * showtimes from `GET /showtimes?movie_id=`, grouped by day with the same
 * ticket stubs the browse page uses.
 */

import { Link, useParams } from 'react-router-dom'

import { Skeleton } from '@/components/ui/skeleton'
import { formatDay } from '@/lib/time'

import { LoadError } from './LoadError'
import { MoviePoster } from './MoviePoster'
import {
  messageForLoadError,
  useMovieDetails,
  useMovieShowtimes,
} from './queries'
import { groupShowtimesByDay } from './showtimeGroups'
import { ShowtimeCard } from './ShowtimeCard'

/**
 * Read a route parameter as a movie id.
 *
 * @param value - `useParams().movieId`, possibly `undefined` on a mistyped URL.
 * @returns The positive integer id, or `null` when the value cannot be one.
 */
function parseMovieId(value: string | undefined): number | null {
  if (value === undefined || !/^\d+$/.test(value)) {
    return null
  }
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

/**
 * The loading state of the movie page.
 *
 * @returns A skeleton for the poster, title, facts and description.
 */
function MovieSkeleton() {
  return (
    <div role="status" className="flex flex-col gap-6">
      <span className="sr-only">Loading movie</span>
      <div className="flex flex-col gap-6 sm:flex-row">
        <Skeleton className="aspect-[2/3] w-40 rounded-poster sm:w-56" />
        <div className="flex flex-1 flex-col gap-3">
          <Skeleton className="h-10 w-2/3" />
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-24 w-full max-w-[65ch]" />
        </div>
      </div>
    </div>
  )
}

/** Props of {@link MovieDetails}. */
interface MovieDetailsProps {
  /** The validated movie id. */
  movieId: number
}

/**
 * Render a loaded movie and its day-grouped showtimes.
 *
 * @param props - The movie id.
 * @returns The movie block and the showtimes, with loading, empty and error
 *   states.
 */
function MovieDetails({ movieId }: MovieDetailsProps) {
  const movie = useMovieDetails(movieId)
  const showtimes = useMovieShowtimes(movieId)
  const days = groupShowtimesByDay(showtimes.data?.items ?? [])
  const isLoading = movie.isPending || showtimes.isPending
  const failure = movie.isError
    ? movie.error
    : showtimes.isError
      ? showtimes.error
      : null

  function retry() {
    void movie.refetch()
    void showtimes.refetch()
  }

  return (
    <main
      id="main-content"
      tabIndex={-1}
      className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6"
    >
      {isLoading && <MovieSkeleton />}

      {failure !== null && (
        <LoadError message={messageForLoadError(failure)} onRetry={retry} />
      )}

      {movie.isSuccess && showtimes.isSuccess && (
        <>
          <div className="flex flex-col gap-6 sm:flex-row sm:gap-8">
            <MoviePoster
              src={movie.data.poster_url}
              title={movie.data.title}
              className="w-40 sm:w-56"
            />
            <div className="flex min-w-0 flex-col gap-3">
              <h1 className="text-6 sm:text-7">{movie.data.title}</h1>
              {movie.data.genres.length > 0 && (
                <p className="text-1 text-muted-foreground">
                  {movie.data.genres.map((genre) => genre.name).join(', ')}
                </p>
              )}
              {movie.data.description.trim() !== '' && (
                <p className="max-w-[65ch] whitespace-pre-line text-2">
                  {movie.data.description}
                </p>
              )}
            </div>
          </div>

          <section aria-labelledby="movie-showtimes" className="mt-10">
            <h2 id="movie-showtimes" className="text-5">
              Showtimes
            </h2>

            {days.length === 0 ? (
              <p className="mt-4 text-2 text-muted-foreground">
                No upcoming showtimes for this movie.
              </p>
            ) : (
              <div className="mt-4 flex flex-col gap-6">
                {days.map(({ day, showtimes: dayShowtimes }) => (
                  <section
                    key={day}
                    aria-labelledby={`showtimes-${day}`}
                    className="border-t border-aisle pt-4"
                  >
                    <h3 id={`showtimes-${day}`} className="text-4">
                      {formatDay(day)}
                    </h3>
                    <ul className="mt-3 flex flex-wrap gap-3">
                      {dayShowtimes.map((showtime) => (
                        <li key={showtime.id}>
                          <ShowtimeCard
                            showtime={showtime}
                            now={showtimes.dataUpdatedAt}
                          />
                        </li>
                      ))}
                    </ul>
                  </section>
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </main>
  )
}

/**
 * Render the movie page for the `:movieId` route parameter.
 *
 * @returns The movie details, an invalid-id message, or the loading skeleton.
 */
export function MoviePage() {
  const { movieId } = useParams<{ movieId: string }>()
  const id = parseMovieId(movieId)

  if (id === null) {
    return (
      <main
        id="main-content"
        tabIndex={-1}
        className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-4 py-10 sm:px-6"
      >
        <h1 className="text-6">Movie not found</h1>
        <p className="text-2">That movie is no longer available.</p>
        <Link
          to="/"
          className="w-fit text-ink underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink"
        >
          Browse showtimes
        </Link>
      </main>
    )
  }

  return <MovieDetails movieId={id} />
}
