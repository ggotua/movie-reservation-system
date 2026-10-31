import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { config } from '@/lib/config'
import { clearToken, getToken, setToken } from '@/lib/tokenStorage'
import { server } from '@/test/server'

import { ApiError, registerUnauthorizedHandler, request } from './client'
import type { User } from './types'

/** The API base URL without a trailing slash, as the client builds it. */
const BASE_URL = config.apiBaseUrl.replace(/\/+$/, '')

/** A realistic `UserOut`, copied from the backend schema. */
const USER: User = {
  id: 7,
  email: 'ana@example.com',
  role: 'user',
  created_at: '2026-10-30T09:00:00Z',
}

afterEach(() => {
  clearToken()
})

describe('api client', () => {
  it('apiClient_sends_bearer_token_and_parses_standard_error_shape', async () => {
    setToken('test.jwt.token')
    const authorizationHeaders: (string | null)[] = []

    server.use(
      http.get(`${BASE_URL}/auth/me`, ({ request: httpRequest }) => {
        authorizationHeaders.push(httpRequest.headers.get('Authorization'))
        return HttpResponse.json(USER)
      }),
      http.get(`${BASE_URL}/movies/999`, () =>
        HttpResponse.json(
          {
            error: {
              code: 'MOVIE_NOT_FOUND',
              message: 'Movie 999 does not exist.',
            },
          },
          { status: 404 },
        ),
      ),
    )

    const user = await request<User>('/auth/me')
    expect(user).toEqual(USER)
    expect(authorizationHeaders).toEqual(['Bearer test.jwt.token'])

    const failure = await request('/movies/999').then(
      () => null,
      (reason: unknown) => reason,
    )
    expect(failure).toBeInstanceOf(ApiError)
    expect(failure).toMatchObject({
      status: 404,
      code: 'MOVIE_NOT_FOUND',
      message: 'Movie 999 does not exist.',
    })
  })

  it('apiClient_clears_session_on_401_unauthenticated', async () => {
    setToken('expired.jwt.token')
    const onUnauthorized = vi.fn()
    const unsubscribe = registerUnauthorizedHandler(onUnauthorized)

    server.use(
      http.get(`${BASE_URL}/auth/me`, () =>
        HttpResponse.json(
          {
            error: {
              code: 'UNAUTHENTICATED',
              message: 'Authentication required.',
            },
          },
          { status: 401 },
        ),
      ),
    )

    try {
      await expect(request('/auth/me')).rejects.toMatchObject({
        status: 401,
        code: 'UNAUTHENTICATED',
      })
      expect(getToken()).toBeNull()
      expect(onUnauthorized).toHaveBeenCalledTimes(1)

      // The unsubscribe function stops a later 401 from notifying again.
      unsubscribe()
      setToken('expired.jwt.token')
      await expect(request('/auth/me')).rejects.toMatchObject({
        status: 401,
        code: 'UNAUTHENTICATED',
      })
      expect(onUnauthorized).toHaveBeenCalledTimes(1)
    } finally {
      unsubscribe()
    }
  })
})
