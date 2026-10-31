/**
 * The single owner of the bearer token in `localStorage` (SPEC-7 FR-11, D3).
 *
 * The auth layer stores the token only through this module, so there is one key
 * and one place that has to tolerate an unavailable storage (private browsing,
 * a browser that blocks site data, a non-browser test environment). Every
 * accessor fails closed: a storage error behaves like "no token", never like a
 * crash.
 */

/** The one `localStorage` key the app uses for the JWT. */
const TOKEN_STORAGE_KEY = 'movie-reservation.access-token'

/**
 * Read the stored token.
 *
 * @returns The token, or `null` when none is stored or `localStorage` is
 *   unavailable.
 *
 * Non-obvious: an empty stored string is reported as `null` because it cannot
 * be a usable bearer token.
 */
export function getToken(): string | null {
  try {
    const token = globalThis.localStorage.getItem(TOKEN_STORAGE_KEY)
    return token === null || token === '' ? null : token
  } catch {
    return null
  }
}

/**
 * Store a token for later requests.
 *
 * @param token - The JWT returned by `POST /auth/login`.
 *
 * Non-obvious: a storage failure is swallowed on purpose. The user stays logged
 * in for the current page and the next reload simply asks them to log in again,
 * which is better than breaking the login flow.
 */
export function setToken(token: string): void {
  try {
    globalThis.localStorage.setItem(TOKEN_STORAGE_KEY, token)
  } catch {
    // Storage is unavailable; the session lasts for this page only.
  }
}

/**
 * Remove the stored token, e.g. on logout or after a 401.
 *
 * Non-obvious: a storage failure is swallowed; there is nothing left to do if
 * the token cannot be removed.
 */
export function clearToken(): void {
  try {
    globalThis.localStorage.removeItem(TOKEN_STORAGE_KEY)
  } catch {
    // Storage is unavailable; nothing to clear.
  }
}
