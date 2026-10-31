/**
 * Component tests for the auth flow (SPEC-7 Section 5.1, tests 15-19).
 *
 * The real `App` is rendered so the router, the query client and the auth
 * provider all take part; only the backend is mocked, with responses shaped
 * like the backend's schemas.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'

import type { User } from '@/api/types'
import { config } from '@/lib/config'
import { clearToken, getToken, setToken } from '@/lib/tokenStorage'
import { server } from '@/test/server'

import App from '@/App'

/** The API base URL without a trailing slash, as the client builds it. */
const BASE_URL = config.apiBaseUrl.replace(/\/+$/, '')

/** A realistic `UserOut`, copied from the backend schema. */
const USER: User = {
  id: 7,
  email: 'ana@example.com',
  role: 'user',
  created_at: '2026-10-30T09:00:00Z',
}

/** The password the mocked backend accepts; long enough for client validation. */
const PASSWORD = 'correct-horse-battery'

/** The token the mocked backend issues. */
const TOKEN_VALUE = 'header.payload.signature'

/** The standard unauthenticated error body. */
const UNAUTHENTICATED_BODY = {
  error: { code: 'UNAUTHENTICATED', message: 'Authentication required.' },
}

/**
 * Render the app at a given URL.
 *
 * @param path - The path (plus query) to start from; BrowserRouter reads it
 *   from `window.history`.
 * @returns The Testing Library render result.
 */
function renderAt(path: string) {
  window.history.replaceState({}, '', path)
  return render(<App />)
}

/**
 * A `POST /auth/login` handler that accepts {@link PASSWORD} for {@link USER}.
 *
 * @returns The MSW handler.
 */
function successfulLoginHandler() {
  return http.post(`${BASE_URL}/auth/login`, async ({ request }) => {
    const body = (await request.json()) as {
      email?: unknown
      password?: unknown
    }
    if (body.email === USER.email && body.password === PASSWORD) {
      return HttpResponse.json({
        access_token: TOKEN_VALUE,
        token_type: 'bearer',
        expires_in: 3600,
      })
    }
    return HttpResponse.json(
      {
        error: {
          code: 'INVALID_CREDENTIALS',
          message: 'Incorrect email or password.',
        },
      },
      { status: 401 },
    )
  })
}

afterEach(() => {
  clearToken()
})

describe('auth', () => {
  it('login_success_stores_token_and_redirects_to_next_path', async () => {
    const user = userEvent.setup()
    server.use(
      successfulLoginHandler(),
      http.get(`${BASE_URL}/auth/me`, () => HttpResponse.json(USER)),
    )

    renderAt('/login?next=%2Freservations')
    await user.type(screen.getByLabelText('Email'), USER.email)
    await user.type(screen.getByLabelText('Password'), PASSWORD)
    await user.click(screen.getByRole('button', { name: 'Log in' }))

    expect(
      await screen.findByRole('heading', { level: 1, name: 'My reservations' }),
    ).toBeInTheDocument()
    expect(getToken()).toBe(TOKEN_VALUE)
    expect(window.location.pathname).toBe('/reservations')
  })

  it('login_wrong_credentials_shows_message_and_stores_nothing', async () => {
    const user = userEvent.setup()
    server.use(successfulLoginHandler())

    renderAt('/login')
    await user.type(screen.getByLabelText('Email'), USER.email)
    await user.type(screen.getByLabelText('Password'), 'not-the-password')
    await user.click(screen.getByRole('button', { name: 'Log in' }))

    expect(
      await screen.findByText('Incorrect email or password.'),
    ).toBeInTheDocument()
    expect(getToken()).toBeNull()
    expect(window.location.pathname).toBe('/login')
  })

  it('signup_validation_and_duplicate_email_message', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(`${BASE_URL}/auth/signup`, () =>
        HttpResponse.json(
          {
            error: {
              code: 'EMAIL_ALREADY_REGISTERED',
              message: 'An account with this email already exists.',
            },
          },
          { status: 409 },
        ),
      ),
    )

    renderAt('/signup')
    const emailInput = screen.getByLabelText('Email')
    const passwordInput = screen.getByLabelText('Password')

    await user.type(emailInput, 'not-an-email')
    await user.type(passwordInput, 'short')
    await user.click(screen.getByRole('button', { name: 'Sign up' }))

    expect(screen.getByText('Enter a valid email address.')).toBeInTheDocument()
    expect(
      screen.getByText('Password must be at least 8 characters.'),
    ).toBeInTheDocument()

    await user.clear(passwordInput)
    await user.type(passwordInput, 'a'.repeat(73))
    await user.click(screen.getByRole('button', { name: 'Sign up' }))

    expect(
      screen.getByText('Password must be at most 72 bytes.'),
    ).toBeInTheDocument()

    await user.clear(emailInput)
    await user.type(emailInput, USER.email)
    await user.clear(passwordInput)
    await user.type(passwordInput, PASSWORD)
    await user.click(screen.getByRole('button', { name: 'Sign up' }))

    expect(
      await screen.findByText(
        'An account with this email already exists. Log in instead.',
      ),
    ).toBeInTheDocument()
    expect(getToken()).toBeNull()
  })

  it('protected_routes_redirect_anonymous_to_login_with_next', async () => {
    const user = userEvent.setup()
    server.use(
      successfulLoginHandler(),
      http.get(`${BASE_URL}/auth/me`, () => HttpResponse.json(USER)),
    )

    renderAt('/checkout/5')

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Log in' }),
    ).toBeInTheDocument()
    expect(window.location.pathname).toBe('/login')
    expect(new URLSearchParams(window.location.search).get('next')).toBe(
      '/checkout/5',
    )

    await user.type(screen.getByLabelText('Email'), USER.email)
    await user.type(screen.getByLabelText('Password'), PASSWORD)
    await user.click(screen.getByRole('button', { name: 'Log in' }))

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Checkout' }),
    ).toBeInTheDocument()
    expect(window.location.pathname).toBe('/checkout/5')
  })


  it('session_restored_from_stored_token_and_invalid_token_cleared', async () => {
    server.use(
      http.get(`${BASE_URL}/auth/me`, ({ request }) => {
        if (request.headers.get('Authorization') === `Bearer ${TOKEN_VALUE}`) {
          return HttpResponse.json(USER)
        }
        return HttpResponse.json(UNAUTHENTICATED_BODY, { status: 401 })
      }),
    )

    setToken(TOKEN_VALUE)
    const firstRender = renderAt('/reservations')

    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: 'My reservations',
      }),
    ).toBeInTheDocument()
    expect(window.location.pathname).toBe('/reservations')
    firstRender.unmount()

    clearToken()
    setToken('expired.jwt.token')
    renderAt('/reservations')

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Log in' }),
    ).toBeInTheDocument()
    expect(
      screen.getByText('Your session has ended. Log in to continue.'),
    ).toBeInTheDocument()
    expect(getToken()).toBeNull()
    expect(window.location.pathname).toBe('/login')
  })
})

