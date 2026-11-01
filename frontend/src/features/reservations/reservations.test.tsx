/**
 * Component tests for checkout and the reservations list (SPEC-7 Section 5.1,
 * tests 31-36).
 *
 * The real `App` is rendered, so the router, query client, auth provider and
 * shell take part; only the backend is mocked, with responses shaped like the
 * backend schemas. The countdown tests use fake timers, because the hold window
 * is measured in minutes and the expiry rule is what they check.
 */

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type {
  Page,
  Reservation,
  ReservationListItem,
  ReservationStatus,
  Seat,
  SeatMap,
  Showtime,
  User,
} from '@/api/types'
import { config } from '@/lib/config'
import { formatMoney } from '@/lib/money'
import { addDays, todayInCinema } from '@/lib/time'
import { clearToken, setToken } from '@/lib/tokenStorage'
import { server } from '@/test/server'

import App from '@/App'

/** The API base URL without a trailing slash, as the client builds it. */
const BASE_URL = config.apiBaseUrl.replace(/\/+$/, '')

/** The showtime the reservation belongs to. */
const SHOWTIME_ID = 101

/** The reservation every test opens. */
const RESERVATION_ID = 55

/** A day that is never "today", so the showtime counts as upcoming. */
const TOMORROW = addDays(todayInCinema(), 1)

/** A stored token string; its value is irrelevant once `/auth/me` is mocked. */
const TOKEN_VALUE = 'reservations.jwt.token'

/** An account as `GET /auth/me` returns it. */
const USER: User = {
  id: 7,
  email: 'ada@example.com',
  role: 'user',
  created_at: '2026-01-01T09:00:00Z',
}

/** The showtime both screens show. */
const SHOWTIME: Showtime = {
  id: SHOWTIME_ID,
  starts_at: `${TOMORROW}T19:00:00Z`,
  price_cents: 1250,
  movie: { id: 3, title: 'The Long Quiet', poster_url: null },
  screen: { id: 1, name: 'Screen 1' },
}

/** The instant the reservation was created, for the hold-window arithmetic. */
const CREATED_AT = '2026-01-01T10:00:00Z'

/** Ten minutes after {@link CREATED_AT}, the backend's hold window. */
const EXPIRES_AT = '2026-01-01T10:10:00Z'

/**
 * The stub's total as Testing Library sees it.
 *
 * Non-obvious: `Intl` separates the currency from the amount with a
 * non-breaking space, and Testing Library compares against the whitespace it
 * has already normalized, so the expectation is normalized the same way.
 */
const TOTAL_TEXT = formatMoney(2500).replace(/\u00a0/g, ' ')

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
 * Build a reservation as `GET /reservations/{id}` returns it.
 *
 * @param overrides - Fields to replace, e.g. `{ status: 'confirmed' }`.
 * @returns A two-seat reservation for {@link SHOWTIME_ID}.
 */
function makeReservation(
  overrides: Partial<ReservationListItem> = {},
): Reservation {
  return {
    id: RESERVATION_ID,
    showtime_id: SHOWTIME_ID,
    status: 'held',
    seats: [
      { seat_id: 1, row_label: 'A', seat_number: 1 },
      { seat_id: 2, row_label: 'A', seat_number: 2 },
    ],
    total_cents: 2500,
    created_at: CREATED_AT,
    expires_at: EXPIRES_AT,
    confirmed_at: null,
    cancelled_at: null,
    ...overrides,
  }
}

/**
 * Build a list row: a reservation with its showtime embedded.
 *
 * @param overrides - Fields to replace, e.g. `{ status: 'confirmed' }`.
 * @returns The row object.
 */
function makeListItem(
  overrides: Partial<ReservationListItem> = {},
): ReservationListItem {
  return { ...makeReservation(overrides), showtime: SHOWTIME }
}

/**
 * Wrap rows in the API's list envelope.
 *
 * @param items - The rows of the page.
 * @param total - How many rows match the filter in total; defaults to one page.
 * @returns The envelope `GET /reservations` returns.
 */
function makePage(
  items: ReservationListItem[],
  total = items.length,
): Page<ReservationListItem> {
  return { items, total, limit: 10, offset: 0 }
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

/**
 * Build a seat map of two rows of three seats, all available.
 *
 * @returns The map the seat screen renders after a release.
 */
function makeSeatMap(): SeatMap {
  const seats: Seat[] = []
  for (const rowLabel of ['A', 'B']) {
    for (let number = 1; number <= 3; number += 1) {
      seats.push({
        seat_id: (rowLabel === 'A' ? 0 : 3) + number,
        row_label: rowLabel,
        seat_number: number,
        seat_type: 'standard',
        state: 'available',
      })
    }
  }
  return {
    showtime_id: SHOWTIME_ID,
    screen: { id: 1, name: 'Screen 1', rows: 2, seats_per_row: 3 },
    seats,
  }
}

/** The `GET /auth/me` handler, for the tests that need a session. */
function currentUserHandler() {
  return http.get(`${BASE_URL}/auth/me`, () => HttpResponse.json(USER))
}

/** The `GET /showtimes/{id}` handler, serving the fixture showtime. */
function showtimeHandler() {
  return http.get(`${BASE_URL}/showtimes/${SHOWTIME_ID}`, () =>
    HttpResponse.json(SHOWTIME),
  )
}

/**
 * The `GET /reservations/{id}` handler.
 *
 * @param read - Builds the body on every read, so a test can change the status
 *   the server reports between reads.
 * @returns The MSW handler.
 */
function reservationHandler(read: () => Reservation) {
  return http.get(`${BASE_URL}/reservations/${RESERVATION_ID}`, () =>
    HttpResponse.json(read()),
  )
}

afterEach(() => {
  // A leftover token would silently change the session of the next test.
  clearToken()
})

describe('checkout', () => {
  it('checkout_confirm_success_shows_confirmed_state', async () => {
    setToken(TOKEN_VALUE)
    const user = userEvent.setup()
    let confirmCalls = 0
    server.use(
      currentUserHandler(),
      showtimeHandler(),
      reservationHandler(() => makeReservation()),
      http.post(`${BASE_URL}/reservations/${RESERVATION_ID}/confirm`, () => {
        confirmCalls += 1
        return HttpResponse.json(
          makeReservation({
            status: 'confirmed',
            expires_at: null,
            confirmed_at: '2026-01-01T10:01:00Z',
          }),
        )
      }),
    )

    renderAt(`/checkout/${RESERVATION_ID}`)

    // The stub shows what is being bought, from the server's own numbers.
    expect(
      await screen.findByRole('heading', {
        level: 2,
        name: SHOWTIME.movie.title,
      }),
    ).toBeInTheDocument()
    expect(screen.getByText('A1, A2')).toBeInTheDocument()
    expect(screen.getByText(TOTAL_TEXT)).toBeInTheDocument()
    expect(screen.getByText(/^Seats held for \d\d:\d\d$/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Confirm booking' }))

    // The server's confirmed reservation replaces the held one in place.
    expect(await screen.findByText('Booking confirmed')).toBeInTheDocument()
    expect(confirmCalls).toBe(1)
    expect(
      screen.queryByRole('button', { name: 'Confirm booking' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Release seats' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByText(/^Seats held for/)).not.toBeInTheDocument()
    // The header links to the list too, so only the confirmed stub's own link
    // is checked here.
    expect(
      within(screen.getByRole('main')).getByRole('link', {
        name: 'My reservations',
      }),
    ).toHaveAttribute('href', '/reservations')
  })

  it('checkout_countdown_expiry_disables_confirm_and_uses_server_status', async () => {
    // The hold runs out during the test, so the clock and the one-second tick
    // are faked. `shouldAdvanceTime` keeps Testing Library's own waiting
    // working, and `Date` is faked so `expires_at` and the countdown agree.
    vi.useFakeTimers({
      shouldAdvanceTime: true,
      toFake: [
        'setTimeout',
        'setInterval',
        'clearTimeout',
        'clearInterval',
        'Date',
      ],
    })

    try {
      setToken(TOKEN_VALUE)
      const expiresAt = new Date(Date.now() + 5000).toISOString()
      let reads = 0
      server.use(
        currentUserHandler(),
        showtimeHandler(),
        http.get(`${BASE_URL}/reservations/${RESERVATION_ID}`, () => {
          reads += 1
          // The server is the authority on expiry: the first read is a live
          // hold, every later read reports it expired.
          const status: ReservationStatus = reads === 1 ? 'held' : 'expired'
          return HttpResponse.json(
            makeReservation({ status, expires_at: expiresAt }),
          )
        }),
      )

      renderAt(`/checkout/${RESERVATION_ID}`)

      expect(
        await screen.findByRole('button', { name: 'Confirm booking' }),
      ).toBeEnabled()
      expect(screen.getByText(/^Seats held for \d\d:\d\d$/)).toBeInTheDocument()

      // One second before the expiry the hold is still active.
      await vi.advanceTimersByTimeAsync(4000)
      expect(
        screen.getByRole('button', { name: 'Confirm booking' }),
      ).toBeEnabled()

      // Past the expiry the page refetches once and renders what it hears.
      await vi.advanceTimersByTimeAsync(1500)
      expect(
        await screen.findByText('This hold has expired. Choose seats again.'),
      ).toBeInTheDocument()
      expect(
        screen.queryByRole('button', { name: 'Confirm booking' }),
      ).not.toBeInTheDocument()
      expect(
        screen.queryByRole('button', { name: 'Release seats' }),
      ).not.toBeInTheDocument()
      expect(
        screen.getByRole('link', { name: 'Choose seats again' }),
      ).toHaveAttribute('href', `/showtimes/${SHOWTIME_ID}`)
      expect(reads).toBeGreaterThan(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('checkout_release_returns_to_seat_map', async () => {
    setToken(TOKEN_VALUE)
    const user = userEvent.setup()
    let releaseCalls = 0
    server.use(
      currentUserHandler(),
      showtimeHandler(),
      reservationHandler(() => makeReservation()),
      http.get(`${BASE_URL}/showtimes/${SHOWTIME_ID}/seats`, () =>
        HttpResponse.json(makeSeatMap()),
      ),
      http.post(`${BASE_URL}/reservations/${RESERVATION_ID}/release`, () => {
        releaseCalls += 1
        return HttpResponse.json(
          makeReservation({
            status: 'cancelled',
            expires_at: null,
            cancelled_at: '2026-01-01T10:02:00Z',
          }),
        )
      }),
    )

    renderAt(`/checkout/${RESERVATION_ID}`)

    await user.click(
      await screen.findByRole('button', { name: 'Release seats' }),
    )

    // The seat map is where the seats are chosen again, so the release lands
    // there and says what happened.
    expect(await screen.findByText('Seats released')).toBeInTheDocument()
    expect(releaseCalls).toBe(1)
    expect(window.location.pathname).toBe(`/showtimes/${SHOWTIME_ID}`)
    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: SHOWTIME.movie.title,
      }),
    ).toBeInTheDocument()
  })
})

describe('reservations list', () => {
  it('my_reservations_list_filters_by_status_and_upcoming', async () => {
    setToken(TOKEN_VALUE)
    const user = userEvent.setup()
    const requested: URL[] = []
    server.use(
      currentUserHandler(),
      http.get(`${BASE_URL}/reservations`, ({ request }) => {
        const url = new URL(request.url)
        requested.push(url)
        const status = url.searchParams.get('status')
        // Only the filters matter here, so each request returns one row
        // carrying the status that was asked for.
        return HttpResponse.json(
          makePage([
            makeListItem({
              status: status === 'confirmed' ? 'confirmed' : 'held',
              confirmed_at:
                status === 'confirmed' ? '2026-01-01T10:01:00Z' : null,
            }),
          ]),
        )
      }),
    )

    renderAt('/reservations')

    const firstList = await screen.findByRole('list', {
      name: 'Reservations',
    })
    expect(within(firstList).getByText('Held')).toBeInTheDocument()

    // Unset filters are not sent at all, and one page is asked for.
    expect(requested[0]?.searchParams.get('status')).toBeNull()
    expect(requested[0]?.searchParams.get('upcoming')).toBeNull()
    expect(requested[0]?.searchParams.get('limit')).toBe('10')
    expect(requested[0]?.searchParams.get('offset')).toBe('0')

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Status' }),
      'confirmed',
    )
    await waitFor(() => {
      expect(requested.at(-1)?.searchParams.get('status')).toBe('confirmed')
    })
    // A filter change starts again from the first page.
    expect(requested.at(-1)?.searchParams.get('offset')).toBe('0')

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Time' }),
      'upcoming',
    )
    await waitFor(() => {
      expect(requested.at(-1)?.searchParams.get('upcoming')).toBe('true')
    })
    expect(requested.at(-1)?.searchParams.get('status')).toBe('confirmed')
    expect(requested.at(-1)?.searchParams.get('limit')).toBe('10')

    const filteredList = await screen.findByRole('list', {
      name: 'Reservations',
    })
    expect(within(filteredList).getByText('Confirmed')).toBeInTheDocument()
    expect(within(filteredList).getByText('A1, A2')).toBeInTheDocument()
  })

  it('cancel_confirmed_upcoming_asks_confirmation_and_updates_list', async () => {
    setToken(TOKEN_VALUE)
    const user = userEvent.setup()
    let isCancelled = false
    server.use(
      currentUserHandler(),
      http.get(`${BASE_URL}/reservations`, () =>
        HttpResponse.json(
          makePage([
            makeListItem(
              isCancelled
                ? {
                    status: 'cancelled',
                    expires_at: null,
                    cancelled_at: '2026-01-01T10:05:00Z',
                  }
                : {
                    status: 'confirmed',
                    expires_at: null,
                    confirmed_at: '2026-01-01T10:01:00Z',
                  },
            ),
          ]),
        ),
      ),
      http.post(`${BASE_URL}/reservations/${RESERVATION_ID}/cancel`, () => {
        isCancelled = true
        return HttpResponse.json(
          makeReservation({
            status: 'cancelled',
            expires_at: null,
            cancelled_at: '2026-01-01T10:05:00Z',
          }),
        )
      }),
    )

    renderAt('/reservations')

    await user.click(
      await screen.findByRole('button', { name: 'Cancel booking' }),
    )

    // Nothing is sent until the question is answered.
    const dialog = await screen.findByRole('alertdialog')
    expect(
      within(dialog).getByText('Cancel this booking?'),
    ).toBeInTheDocument()
    expect(isCancelled).toBe(false)

    await user.click(
      within(dialog).getByRole('button', { name: 'Cancel booking' }),
    )

    expect(await screen.findByText('Booking cancelled')).toBeInTheDocument()
    expect(isCancelled).toBe(true)
    const refreshedList = await screen.findByRole('list', {
      name: 'Reservations',
    })
    expect(within(refreshedList).getByText('Cancelled')).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Cancel booking' }),
    ).not.toBeInTheDocument()
  })

  it('cancel_error_shows_friendly_message', async () => {
    setToken(TOKEN_VALUE)
    const user = userEvent.setup()
    let cancelAttempts = 0
    server.use(
      currentUserHandler(),
      http.get(`${BASE_URL}/reservations`, () =>
        HttpResponse.json(
          makePage([
            makeListItem({
              status: 'confirmed',
              expires_at: null,
              confirmed_at: '2026-01-01T10:01:00Z',
            }),
          ]),
        ),
      ),
      http.post(`${BASE_URL}/reservations/${RESERVATION_ID}/cancel`, () => {
        cancelAttempts += 1
        return errorResponse(
          409,
          'RESERVATION_NOT_CONFIRMED',
          'Only confirmed reservations can be cancelled.',
        )
      }),
    )

    renderAt('/reservations')

    await user.click(
      await screen.findByRole('button', { name: 'Cancel booking' }),
    )
    const dialog = await screen.findByRole('alertdialog')
    await user.click(
      within(dialog).getByRole('button', { name: 'Cancel booking' }),
    )

    // The friendly text for the code is shown, not the server's wording, and
    // the booking stays as the server reported it.
    expect(
      await screen.findByText('Only confirmed bookings can be cancelled.'),
    ).toBeInTheDocument()
    expect(cancelAttempts).toBe(1)
    expect(
      screen.queryByText('Only confirmed reservations can be cancelled.'),
    ).not.toBeInTheDocument()
    const list = screen.getByRole('list', { name: 'Reservations' })
    expect(within(list).getByText('Confirmed')).toBeInTheDocument()
  })
})
