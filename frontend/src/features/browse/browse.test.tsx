/**
 * Component tests for the browse and movie pages (SPEC-7 Section 5.1, tests
 * 20-22).
 *
 * The real `App` is rendered so the router, query client and shell take part;
 * only the backend is mocked, with responses shaped like the backend schemas.
 */

import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, delay, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import type {
  Movie,
  MovieSummary,
  Page,
  ScreenSummary,
  Showtime,
} from '@/api/types'
import { config } from '@/lib/config'
import {
  addDays,
  formatDay,
  formatDayShort,
  formatShowtime,
  todayInCinema,
} from '@/lib/time'
import { server } from '@/test/server'

import App from '@/App'

/** The API base URL without a trailing slash, as the client builds it. */
const BASE_URL = config.apiBaseUrl.replace(/\/+$/, '')

/** Days relative to the real clock, so the strip and the API agree. */
const TODAY = todayInCinema()
const TOMORROW = addDays(TODAY, 1)
const LATER = addDays(TODAY, 2)
const NEXT = addDays(TODAY, 3)

/** Movie summaries as `ShowtimeOut` embeds them. */
const LONG_QUIET: MovieSummary = {
  id: 3,
  title: 'The Long Quiet',
  poster_url: null,
}

const HARBOUR_LIGHTS: MovieSummary = {
  id: 4,
  title: 'Harbour Lights',
  poster_url: 'https://example.com/harbour-lights.jpg',
}

const SCREEN_ONE: ScreenSummary = { id: 1, name: 'Screen 1' }
const SCREEN_TWO: ScreenSummary = { id: 2, name: 'Screen 2' }

/** Props accepted by the showtime fixture helper. */
interface ShowtimeFixture {
  id: number
  startsAt: string
  priceCents: number
  movie: MovieSummary
  screen: ScreenSummary
}

/**
 * Build a `ShowtimeOut` shaped like the backend's response.
 *
 * @param fixture - The id, instant, price and embedded refs.
 * @returns The showtime object.
 */
function makeShowtime(fixture: ShowtimeFixture): Showtime {
  return {
    id: fixture.id,
    starts_at: fixture.startsAt,
    price_cents: fixture.priceCents,
    movie: fixture.movie,
    screen: fixture.screen,
  }
}

/**
 * Build the shared `Page` envelope.
 *
 * @param items - The page's showtimes.
 * @returns The page with `total`, `limit` and `offset` filled in.
 */
function showtimePage(items: Showtime[]): Page<Showtime> {
  return { items, total: items.length, limit: 100, offset: 0 }
}

/**
 * Render the app at a given URL.
 *
 * @param path - The path (plus query) to start from.
 * @returns The Testing Library render result.
 */
function renderAt(path: string) {
  window.history.replaceState({}, '', path)
  return render(<App />)
}

describe('browse', () => {
  it('browse_shows_showtimes_for_selected_date_grouped_by_movie', async () => {
    const user = userEvent.setup()
    const harbourToday = makeShowtime({
      id: 201,
      startsAt: `${TODAY}T14:00:00Z`,
      priceCents: 1000,
      movie: HARBOUR_LIGHTS,
      screen: SCREEN_TWO,
    })
    const longQuietEarly = makeShowtime({
      id: 101,
      startsAt: `${TODAY}T16:30:00Z`,
      priceCents: 1200,
      movie: LONG_QUIET,
      screen: SCREEN_ONE,
    })
    const longQuietLate = makeShowtime({
      id: 102,
      startsAt: `${TODAY}T19:00:00Z`,
      priceCents: 1500,
      movie: LONG_QUIET,
      screen: SCREEN_ONE,
    })
    const harbourTomorrow = makeShowtime({
      id: 202,
      startsAt: `${TOMORROW}T14:00:00Z`,
      priceCents: 1000,
      movie: HARBOUR_LIGHTS,
      screen: SCREEN_TWO,
    })

    server.use(
      http.get(`${BASE_URL}/showtimes`, ({ request }) => {
        const date = new URL(request.url).searchParams.get('date')
        if (date === TOMORROW) {
          return HttpResponse.json(showtimePage([harbourTomorrow]))
        }
        return HttpResponse.json(
          showtimePage([harbourToday, longQuietEarly, longQuietLate]),
        )
      }),
    )

    renderAt('/')

    expect(
      await screen.findByRole('heading', { level: 1, name: formatDay(TODAY) }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: formatDayShort(TODAY) }),
    ).toHaveAttribute('aria-current', 'date')

    const longQuiet = await screen.findByRole('region', {
      name: LONG_QUIET.title,
    })
    expect(
      within(longQuiet)
        .getAllByRole('link')
        .map((link) => link.getAttribute('href')),
    ).toEqual(['/movies/3', '/showtimes/101', '/showtimes/102'])
    expect(
      within(longQuiet).getByText(formatShowtime(longQuietEarly.starts_at)),
    ).toBeInTheDocument()
    expect(
      within(longQuiet).getByText(formatShowtime(longQuietLate.starts_at)),
    ).toBeInTheDocument()

    const harbour = await screen.findByRole('region', {
      name: HARBOUR_LIGHTS.title,
    })
    expect(
      within(harbour)
        .getAllByRole('link')
        .map((link) => link.getAttribute('href')),
    ).toEqual(['/movies/4', '/showtimes/201'])
    expect(
      within(harbour).queryByText(formatShowtime(longQuietEarly.starts_at)),
    ).toBeNull()

    await user.click(
      screen.getByRole('link', { name: formatDayShort(TOMORROW) }),
    )

    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: formatDay(TOMORROW),
      }),
    ).toBeInTheDocument()
    expect(window.location.search).toBe(`?date=${TOMORROW}`)
    expect(
      screen.getByRole('link', { name: formatDayShort(TOMORROW) }),
    ).toHaveAttribute('aria-current', 'date')
    expect(
      await screen.findByRole('region', { name: HARBOUR_LIGHTS.title }),
    ).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: LONG_QUIET.title })).toBeNull()
  })

  it('browse_empty_day_and_error_with_retry', async () => {
    const user = userEvent.setup()
    let failTomorrow = false
    const harbourTomorrow = makeShowtime({
      id: 202,
      startsAt: `${TOMORROW}T14:00:00Z`,
      priceCents: 1000,
      movie: HARBOUR_LIGHTS,
      screen: SCREEN_TWO,
    })

    server.use(
      http.get(`${BASE_URL}/showtimes`, ({ request }) => {
        const date = new URL(request.url).searchParams.get('date')
        if (date === TOMORROW) {
          if (failTomorrow) {
            return HttpResponse.json(
              {
                error: {
                  code: 'INTERNAL_ERROR',
                  message: 'The server failed.',
                },
              },
              { status: 500 },
            )
          }
          return HttpResponse.json(showtimePage([harbourTomorrow]))
        }
        return HttpResponse.json(showtimePage([]))
      }),
    )

    renderAt('/')

    expect(
      await screen.findByText('No showtimes on this day. Pick another date.'),
    ).toBeInTheDocument()

    failTomorrow = true
    await user.click(
      screen.getByRole('link', { name: formatDayShort(TOMORROW) }),
    )

    expect(
      await screen.findByText('Something went wrong on our side. Try again.'),
    ).toBeInTheDocument()

    failTomorrow = false
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(
      await screen.findByRole('region', { name: HARBOUR_LIGHTS.title }),
    ).toBeInTheDocument()
    expect(
      screen.queryByText('Something went wrong on our side. Try again.'),
    ).toBeNull()
  })

  it('browse_shows_waking_up_notice_after_four_seconds_and_hides_it', async () => {
    // The notice and MSW's delayed response both ride on `setTimeout`, so only
    // the timers are faked: advancing the clock drives the whole sequence, and
    // the test never waits four real seconds.
    vi.useFakeTimers({
      toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval'],
    })

    try {
      const harbourToday = makeShowtime({
        id: 201,
        startsAt: `${TODAY}T14:00:00Z`,
        priceCents: 1000,
        movie: HARBOUR_LIGHTS,
        screen: SCREEN_TWO,
      })
      const notice = 'Waking up the server. This can take up to a minute.'
      // The second mount is the failure case; the first one succeeds.
      let shouldFail = false

      /**
       * Move the fake clock forward and let React and TanStack Query settle.
       *
       * @param milliseconds - How far to advance the fake clock.
       */
      const advanceBy = async (milliseconds: number): Promise<void> => {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(milliseconds)
        })
      }

      server.use(
        http.get(`${BASE_URL}/showtimes`, async () => {
          // The server sleeps: every answer takes longer than the notice delay.
          await delay(5000)
          if (shouldFail) {
            return HttpResponse.json(
              {
                error: {
                  code: 'INTERNAL_ERROR',
                  message: 'The server failed.',
                },
              },
              { status: 500 },
            )
          }
          return HttpResponse.json(showtimePage([harbourToday]))
        }),
      )

      const firstMount = renderAt('/')

      // At 3.9 s the request is still pending but the notice is not due yet.
      await advanceBy(3900)
      expect(screen.queryByText(notice)).toBeNull()

      // At 4.1 s the wait is admitted, in a politely announced region.
      await advanceBy(200)
      expect(screen.getByText(notice)).toHaveAttribute('role', 'status')

      // The data arrives: the notice goes away with the loading state.
      await advanceBy(1000)
      expect(
        screen.getByRole('region', { name: HARBOUR_LIGHTS.title }),
      ).toBeInTheDocument()
      expect(screen.queryByText(notice)).toBeNull()

      // A failed request behaves the same way: the notice shows, then goes.
      firstMount.unmount()
      shouldFail = true
      renderAt('/')

      await advanceBy(3900)
      expect(screen.queryByText(notice)).toBeNull()

      await advanceBy(200)
      expect(screen.getByText(notice)).toBeInTheDocument()

      await advanceBy(1000)
      expect(
        screen.getByText('Something went wrong on our side. Try again.'),
      ).toBeInTheDocument()
      expect(screen.queryByText(notice)).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('movie page', () => {
  it('movie_page_lists_its_upcoming_showtimes', async () => {
    const movie: Movie = {
      id: 3,
      title: 'The Long Quiet',
      description:
        'A projectionist keeps the last reel running through the night.',
      poster_url: null,
      genres: [
        { id: 1, name: 'Drama' },
        { id: 2, name: 'Mystery' },
      ],
      created_at: '2026-10-01T09:00:00Z',
      updated_at: '2026-10-20T09:00:00Z',
    }
    const firstDayEarly = makeShowtime({
      id: 301,
      startsAt: `${LATER}T16:30:00Z`,
      priceCents: 1200,
      movie: LONG_QUIET,
      screen: SCREEN_ONE,
    })
    const firstDayLate = makeShowtime({
      id: 302,
      startsAt: `${LATER}T19:00:00Z`,
      priceCents: 1200,
      movie: LONG_QUIET,
      screen: SCREEN_TWO,
    })
    const secondDay = makeShowtime({
      id: 303,
      startsAt: `${NEXT}T16:30:00Z`,
      priceCents: 1500,
      movie: LONG_QUIET,
      screen: SCREEN_ONE,
    })

    server.use(
      http.get(`${BASE_URL}/movies/3`, () => HttpResponse.json(movie)),
      http.get(`${BASE_URL}/showtimes`, () =>
        HttpResponse.json(
          showtimePage([firstDayEarly, firstDayLate, secondDay]),
        ),
      ),
    )

    renderAt('/movies/3')

    expect(
      await screen.findByRole('heading', { level: 1, name: movie.title }),
    ).toBeInTheDocument()
    expect(screen.getByText(movie.description)).toBeInTheDocument()
    expect(screen.getByText('Drama, Mystery')).toBeInTheDocument()

    const firstDay = await screen.findByRole('region', {
      name: formatDay(LATER),
    })
    expect(
      within(firstDay)
        .getAllByRole('link')
        .map((link) => link.getAttribute('href')),
    ).toEqual(['/showtimes/301', '/showtimes/302'])
    expect(
      within(firstDay).getByText(formatShowtime(firstDayEarly.starts_at)),
    ).toBeInTheDocument()

    const secondDayRegion = await screen.findByRole('region', {
      name: formatDay(NEXT),
    })
    expect(
      within(secondDayRegion).getByRole('link', {
        name: new RegExp(formatShowtime(secondDay.starts_at)),
      }),
    ).toHaveAttribute('href', '/showtimes/303')
  })
})
