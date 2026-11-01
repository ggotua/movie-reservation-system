/**
 * Component tests for the seat map and hold flow (SPEC-7 Section 5.1, tests
 * 23-30).
 *
 * The real `App` is rendered so the router, query client, auth provider and
 * shell take part; only the backend is mocked, with responses shaped like the
 * backend schemas. Seats are always found by their accessible name, because
 * that name is what a screen reader user hears and what FR-3 promises.
 */

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { UserEvent } from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type {
  Reservation,
  Seat,
  SeatMap,
  SeatState,
  Showtime,
  User,
} from '@/api/types'
import { config } from '@/lib/config'
import { addDays, todayInCinema } from '@/lib/time'
import { clearToken, setToken } from '@/lib/tokenStorage'
import { server } from '@/test/server'

import App from '@/App'

/** The API base URL without a trailing slash, as the client builds it. */
const BASE_URL = config.apiBaseUrl.replace(/\/+$/, '')

/** The showtime every test opens. */
const SHOWTIME_ID = 101

/** A day that is never "today", so a showtime on it has not started. */
const TOMORROW = addDays(todayInCinema(), 1)

/** Yesterday, for the started-showtime test. */
const YESTERDAY = addDays(todayInCinema(), -1)

/** A stored token string; its value is irrelevant once `/auth/me` is mocked. */
const TOKEN_VALUE = 'seats.jwt.token'

/** An account as `GET /auth/me` returns it. */
const USER: User = {
  id: 7,
  email: 'ada@example.com',
  role: 'user',
  created_at: '2026-01-01T09:00:00Z',
}

/** The showtime the seat map belongs to. */
const SHOWTIME: Showtime = {
  id: SHOWTIME_ID,
  starts_at: `${TOMORROW}T19:00:00Z`,
  price_cents: 1250,
  movie: { id: 3, title: 'The Long Quiet', poster_url: null },
  screen: { id: 1, name: 'Screen 1' },
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

/**
 * Build a seat map of a rectangular screen.
 *
 * @param states - State overrides keyed by seat label, e.g. `{ B2: 'held' }`.
 *   Every other seat is `available`.
 * @returns A map of three rows of four seats, with ids `1` to `12`.
 *
 * Non-obvious: the last seat of each row is an accessible seat, which is where
 * a real screen puts them; only the seat type differs, the state rules do not.
 */
function makeSeatMap(
  states: Readonly<Record<string, SeatState>> = {},
): SeatMap {
  const rows = 3
  const seatsPerRow = 4
  const seats: Seat[] = []

  for (let rowIndex = 0; rowIndex < rows; rowIndex += 1) {
    const rowLabel = String.fromCharCode('A'.charCodeAt(0) + rowIndex)
    for (let number = 1; number <= seatsPerRow; number += 1) {
      seats.push({
        seat_id: rowIndex * seatsPerRow + number,
        row_label: rowLabel,
        seat_number: number,
        seat_type: number === seatsPerRow ? 'accessible' : 'standard',
        state: states[`${rowLabel}${number}`] ?? 'available',
      })
    }
  }

  return {
    showtime_id: SHOWTIME_ID,
    screen: {
      id: 1,
      name: 'Screen 1',
      rows,
      seats_per_row: seatsPerRow,
    },
    seats,
  }
}

/**
 * The accessible name of a seat button.
 *
 * @param label - The seat label, e.g. `'B2'`.
 * @param state - The state word in the name; `'available'` by default.
 * @returns The name as `SeatGrid` builds it, e.g. `Row B, seat 2, available`.
 */
function seatButtonName(label: string, state = 'available'): string {
  return `Row ${label[0]}, seat ${label.slice(1)}, ${state}`
}

/**
 * Build a `held` reservation as `POST /reservations` returns it.
 *
 * @param id - The reservation id.
 * @param seatIds - The held seat ids, in row order.
 * @returns The reservation object.
 */
function makeReservation(id: number, seatIds: readonly number[]): Reservation {
  return {
    id,
    showtime_id: SHOWTIME_ID,
    status: 'held',
    seats: seatIds.map((seatId) => ({
      seat_id: seatId,
      row_label: 'A',
      seat_number: seatId,
    })),
    total_cents: SHOWTIME.price_cents * seatIds.length,
    created_at: '2026-01-01T10:00:00Z',
    expires_at: '2026-01-01T10:10:00Z',
    confirmed_at: null,
    cancelled_at: null,
  }
}

/**
 * Build the standard error body.
 *
 * @param status - The HTTP status to send.
 * @param code - The backend error code.
 * @param message - The backend message.
 * @returns A JSON error response.
 */
function errorResponse(status: number, code: string, message: string) {
  return HttpResponse.json({ error: { code, message } }, { status })
}

/** The `GET /showtimes/{id}` handler, serving the fixture showtime. */
function showtimeHandler(showtime: Showtime = SHOWTIME) {
  return http.get(`${BASE_URL}/showtimes/${SHOWTIME_ID}`, () =>
    HttpResponse.json(showtime),
  )
}

/** The `GET /showtimes/{id}/seats` handler, serving a fixed map. */
function seatMapHandler(seatMap: SeatMap) {
  return http.get(`${BASE_URL}/showtimes/${SHOWTIME_ID}/seats`, () =>
    HttpResponse.json(seatMap),
  )
}

/** The `GET /auth/me` handler, for the tests that need a session. */
function currentUserHandler() {
  return http.get(`${BASE_URL}/auth/me`, () => HttpResponse.json(USER))
}

/**
 * Click a seat by label, waiting for the grid to appear.
 *
 * @param user - The `userEvent` instance of the test.
 * @param label - The seat label, e.g. `'A1'`; it must still be `available`.
 */
async function clickSeat(user: UserEvent, label: string) {
  await user.click(
    await screen.findByRole('button', { name: seatButtonName(label) }),
  )
}

afterEach(() => {
  // A leftover token would silently change the session of the next test.
  clearToken()
})


describe('seat map', () => {
  it('seat_map_renders_states_with_accessible_labels', async () => {
    const user = userEvent.setup()
    server.use(
      showtimeHandler(),
      seatMapHandler(makeSeatMap({ A2: 'held', A3: 'taken' })),
    )

    renderAt(`/showtimes/${SHOWTIME_ID}`)

    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: SHOWTIME.movie.title,
      }),
    ).toBeInTheDocument()

    const availableSeat = await screen.findByRole('button', {
      name: seatButtonName('A1'),
    })
    expect(availableSeat).toBeEnabled()
    expect(availableSeat).toHaveAttribute('aria-pressed', 'false')

    const heldSeat = screen.getByRole('button', {
      name: seatButtonName('A2', 'held'),
    })
    expect(heldSeat).toBeDisabled()
    expect(heldSeat).not.toHaveAttribute('aria-pressed')

    const takenSeat = screen.getByRole('button', {
      name: seatButtonName('A3', 'taken'),
    })
    expect(takenSeat).toBeDisabled()

    // Every seat is reachable and named, whatever its state.
    expect(
      screen.getAllByRole('button', { name: /^Row [A-C], seat \d, / }),
    ).toHaveLength(12)

    const legend = screen.getByRole('list', { name: 'Seat states' })
    expect(within(legend).getByText('Available')).toBeInTheDocument()
    expect(within(legend).getByText('Selected')).toBeInTheDocument()
    expect(within(legend).getByText('Held')).toBeInTheDocument()
    expect(within(legend).getByText('Taken')).toBeInTheDocument()

    // Space activates a focused seat, and the name reports the selection.
    availableSeat.focus()
    await user.keyboard(' ')
    expect(
      screen.getByRole('button', { name: seatButtonName('A1', 'selected') }),
    ).toHaveAttribute('aria-pressed', 'true')
  })

  it('seat_map_polls_and_removes_selected_seat_that_became_taken', async () => {
    // Fake timers advance the five-second poll without waiting for it. The
    // clock keeps ticking by itself (`shouldAdvanceTime`) so Testing Library's
    // own polling still resolves.
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval'],
    })

    try {
      const user = userEvent.setup({
        advanceTimers: (milliseconds) => {
          vi.advanceTimersByTime(milliseconds)
        },
        delay: null,
      })
      let fetchCount = 0
      server.use(
        showtimeHandler(),
        http.get(`${BASE_URL}/showtimes/${SHOWTIME_ID}/seats`, () => {
          fetchCount += 1
          // Someone else holds B2 between the first load and the poll.
          return HttpResponse.json(
            makeSeatMap(fetchCount === 1 ? {} : { B2: 'held' }),
          )
        }),
      )

      renderAt(`/showtimes/${SHOWTIME_ID}`)

      await clickSeat(user, 'B2')
      expect(
        screen.getByRole('button', { name: seatButtonName('B2', 'selected') }),
      ).toBeInTheDocument()
      expect(screen.getByText('1 seat')).toBeInTheDocument()

      await vi.advanceTimersByTimeAsync(5000)

      expect(
        await screen.findByText(
          'Seat B2 was just taken. We removed it from your selection.',
        ),
      ).toBeInTheDocument()
      expect(
        screen.getByRole('button', { name: seatButtonName('B2', 'held') }),
      ).toBeDisabled()
      expect(screen.getByText('No seats selected.')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Hold seats' })).toBeDisabled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('seat_selection_stops_at_eight_seats', async () => {
    const user = userEvent.setup()
    server.use(showtimeHandler(), seatMapHandler(makeSeatMap()))

    renderAt(`/showtimes/${SHOWTIME_ID}`)

    await screen.findByRole('button', { name: seatButtonName('A1') })
    for (const label of ['A1', 'A2', 'A3', 'A4', 'B1', 'B2', 'B3', 'B4']) {
      await clickSeat(user, label)
    }

    expect(screen.getByText('8 seats')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hold seats' })).toBeEnabled()

    await clickSeat(user, 'C1')

    expect(
      screen.getByText('You can hold at most 8 seats at a time.'),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: seatButtonName('C1') }),
    ).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText('8 seats')).toBeInTheDocument()
  })
})

describe('hold flow', () => {
  it('hold_success_navigates_to_checkout_with_countdown', async () => {
    setToken(TOKEN_VALUE)
    const user = userEvent.setup()
    let postedBody: unknown = null
    server.use(
      currentUserHandler(),
      showtimeHandler(),
      seatMapHandler(makeSeatMap()),
      http.post(`${BASE_URL}/reservations`, async ({ request }) => {
        postedBody = await request.json()
        return HttpResponse.json(makeReservation(55, [1, 2]), { status: 201 })
      }),
    )

    renderAt(`/showtimes/${SHOWTIME_ID}`)

    await clickSeat(user, 'A1')
    await clickSeat(user, 'A2')
    await user.click(screen.getByRole('button', { name: 'Hold seats' }))

    // The checkout screen owns the countdown (tests 31-33); this test only
    // proves the hold reached the API and the app opened that reservation.
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Checkout' }),
    ).toBeInTheDocument()
    expect(window.location.pathname).toBe('/checkout/55')
    expect(postedBody).toEqual({ showtime_id: SHOWTIME_ID, seat_ids: [1, 2] })
  })

  it('hold_conflict_refreshes_map_and_shows_message', async () => {
    setToken(TOKEN_VALUE)
    const user = userEvent.setup()
    let isSeatTaken = false
    server.use(
      currentUserHandler(),
      showtimeHandler(),
      http.get(`${BASE_URL}/showtimes/${SHOWTIME_ID}/seats`, () =>
        HttpResponse.json(makeSeatMap(isSeatTaken ? { A1: 'held' } : {})),
      ),
      http.post(`${BASE_URL}/reservations`, () => {
        isSeatTaken = true
        return errorResponse(
          409,
          'SEAT_UNAVAILABLE',
          'One or more of the requested seats are not available.',
        )
      }),
    )

    renderAt(`/showtimes/${SHOWTIME_ID}`)

    await clickSeat(user, 'A1')
    await user.click(screen.getByRole('button', { name: 'Hold seats' }))

    expect(
      await screen.findByText(
        'Seat A1 was just taken. We removed it from your selection.',
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: seatButtonName('A1', 'held') }),
    ).toBeDisabled()
    expect(screen.getByText('No seats selected.')).toBeInTheDocument()
    expect(window.location.pathname).toBe(`/showtimes/${SHOWTIME_ID}`)
  })

  it('hold_active_hold_exists_links_to_current_hold', async () => {
    setToken(TOKEN_VALUE)
    const user = userEvent.setup()
    server.use(
      currentUserHandler(),
      showtimeHandler(),
      seatMapHandler(makeSeatMap()),
      http.post(`${BASE_URL}/reservations`, () =>
        errorResponse(
          409,
          'ACTIVE_HOLD_EXISTS',
          'You already hold seats for this showtime.',
        ),
      ),
      http.get(`${BASE_URL}/reservations`, () =>
        HttpResponse.json({
          items: [makeReservation(77, [1])],
          total: 1,
          limit: 1,
          offset: 0,
        }),
      ),
    )

    renderAt(`/showtimes/${SHOWTIME_ID}`)

    await clickSeat(user, 'A1')
    await user.click(screen.getByRole('button', { name: 'Hold seats' }))

    expect(
      await screen.findByText(/You already hold seats for this showtime\./),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: 'Go to your current hold' }),
    ).toHaveAttribute('href', '/checkout/77')
  })

  it('hold_requires_login_and_returns_to_showtime', async () => {
    const user = userEvent.setup()
    server.use(showtimeHandler(), seatMapHandler(makeSeatMap()))

    renderAt(`/showtimes/${SHOWTIME_ID}`)

    await clickSeat(user, 'A1')
    await user.click(screen.getByRole('button', { name: 'Hold seats' }))

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Log in' }),
    ).toBeInTheDocument()
    expect(window.location.pathname).toBe('/login')
    expect(new URLSearchParams(window.location.search).get('next')).toBe(
      `/showtimes/${SHOWTIME_ID}`,
    )
  })

  it('started_showtime_disables_hold', async () => {
    const user = userEvent.setup()
    const startedShowtime: Showtime = {
      ...SHOWTIME,
      starts_at: `${YESTERDAY}T19:00:00Z`,
    }
    server.use(showtimeHandler(startedShowtime), seatMapHandler(makeSeatMap()))

    renderAt(`/showtimes/${SHOWTIME_ID}`)

    await clickSeat(user, 'A1')

    const holdButton = screen.getByRole('button', { name: 'Hold seats' })
    expect(holdButton).toBeDisabled()
    expect(holdButton).toHaveAccessibleDescription(
      'This showtime has already started.',
    )
  })
})

describe('seat map failure', () => {
  it('seat_map_error_state_offers_retry', async () => {
    const user = userEvent.setup()
    let isFailing = true
    server.use(
      showtimeHandler(),
      http.get(`${BASE_URL}/showtimes/${SHOWTIME_ID}/seats`, () =>
        isFailing
          ? errorResponse(500, 'INTERNAL_ERROR', 'Unexpected failure.')
          : HttpResponse.json(makeSeatMap()),
      ),
    )

    renderAt(`/showtimes/${SHOWTIME_ID}`)

    expect(
      await screen.findByText('Something went wrong on our side. Try again.'),
    ).toBeInTheDocument()

    isFailing = false
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(
      await screen.findByRole('button', { name: seatButtonName('A1') }),
    ).toBeInTheDocument()
  })
})
