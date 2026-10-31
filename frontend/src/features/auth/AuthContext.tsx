/**
 * The app's single source of truth for the signed-in session (SPEC-7 FR-1,
 * D3).
 *
 * The token lives in React state and is initialised from `tokenStorage`, so a
 * reload restores the session without a round trip. The current account comes
 * from `GET /auth/me` through a TanStack Query hook; when the token is gone the
 * query is disabled. A `401 UNAUTHENTICATED` from any call is reported by
 * `api/client.ts`, which already cleared the token; this provider drops the
 * remaining state and sends the user to login with a short notice.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'

import {
  getCurrentUser,
  login as loginRequest,
  signup as signupRequest,
} from '@/api/auth'
import type { Credentials } from '@/api/auth'
import { registerUnauthorizedHandler } from '@/api/client'
import type { User } from '@/api/types'
import { messageFor } from '@/lib/errorMessages'
import {
  clearToken,
  getToken,
  setToken as persistToken,
} from '@/lib/tokenStorage'

/** A stable TanStack Query cache key for `GET /auth/me`. */
const CURRENT_USER_QUERY_KEY = ['auth', 'me'] as const

/** The session helpers every screen reads through {@link useAuth}. */
export interface AuthContextValue {
  /** The stored JWT, or `null` for an anonymous visitor. */
  token: string | null
  /** The account from `GET /auth/me`; `null` until it has loaded. */
  user: User | null
  /** `true` while a stored token is being checked with `GET /auth/me`. */
  isRestoring: boolean
  /** The restore failure when `/auth/me` failed for a reason other than 401. */
  restoreError: unknown
  /** Retries the `GET /auth/me` call after a network failure. */
  retryRestore: () => void
  /**
   * The short notice the login page should show, e.g. after a 401 ended the
   * session; `null` when there is nothing to say.
   *
   * Non-obvious: it lives here rather than in router state, because both this
   * provider and a protected route redirect to `/login` on a 401 and either
   * navigation may carry the user there.
   */
  notice: string | null
  /**
   * Log in and keep the returned token.
   *
   * @param credentials - The email and password to check.
   *
   * @throws ApiError - `INVALID_CREDENTIALS` (401) for a wrong pair.
   */
  login: (credentials: Credentials) => Promise<void>
  /**
   * Create an account, then sign in with it.
   *
   * The signup endpoint returns the account, not a token, so a second call
   * (`POST /auth/login`) establishes the session (SPEC-2 Section 2.2).
   *
   * @param credentials - The email and password to register.
   *
   * @throws ApiError - `EMAIL_ALREADY_REGISTERED` (409) or
   *   `VALIDATION_ERROR` (422) when the server rejects the input.
   */
  signup: (credentials: Credentials) => Promise<void>
  /** Forget the session and go to the login page. */
  logout: () => void
}

/** `null` outside a provider, so a misplaced `useAuth` fails loudly. */
const AuthContext = createContext<AuthContextValue | null>(null)

/** Props of {@link AuthProvider}. */
export interface AuthProviderProps {
  /** The app below the provider. */
  children: ReactNode
}

/**
 * Load the account belonging to the stored token.
 *
 * @param token - The current token; the query is disabled while it is `null`,
 *   so an anonymous visitor never calls `/auth/me`.
 * @returns The TanStack Query result for `GET /auth/me`. Retries are off: a
 *   401 is final, and a network failure is retried by the user, not silently.
 */
function useCurrentUser(token: string | null) {
  return useQuery({
    queryKey: CURRENT_USER_QUERY_KEY,
    queryFn: getCurrentUser,
    enabled: token !== null,
    retry: false,
  })
}

/**
 * Provide the session to every screen.
 *
 * @param props - The app content to wrap.
 *
 * Non-obvious: the provider must sit inside the router, because a `401
 * UNAUTHENTICATED` navigates to `/login` and because `logout` does too.
 */
export function AuthProvider({ children }: AuthProviderProps) {
  const [token, setTokenState] = useState<string | null>(() => getToken())
  const [notice, setNotice] = useState<string | null>(null)
  const currentUser = useCurrentUser(token)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { refetch } = currentUser

  const login = useCallback(async (credentials: Credentials) => {
    const result = await loginRequest(credentials)
    persistToken(result.access_token)
    setTokenState(result.access_token)
    setNotice(null)
  }, [])

  const signup = useCallback(async (credentials: Credentials) => {
    await signupRequest(credentials)
    const result = await loginRequest(credentials)
    persistToken(result.access_token)
    setTokenState(result.access_token)
    setNotice(null)
  }, [])

  const logout = useCallback(() => {
    clearToken()
    setTokenState(null)
    setNotice(null)
    queryClient.clear()
    navigate('/login', { replace: true })
  }, [navigate, queryClient])

  const retryRestore = useCallback(() => {
    void refetch()
  }, [refetch])

  useEffect(() => {
    return registerUnauthorizedHandler(() => {
      setTokenState(null)
      setNotice(messageFor('UNAUTHENTICATED'))
      queryClient.clear()
      if (window.location.pathname !== '/login') {
        navigate('/login', { replace: true })
      }
    })
  }, [navigate, queryClient])

  const value = useMemo<AuthContextValue>(() => {
    return {
      token,
      user: token === null ? null : (currentUser.data ?? null),
      isRestoring: token !== null && currentUser.isPending,
      restoreError:
        token === null || !currentUser.isError ? null : currentUser.error,
      retryRestore,
      notice,
      login,
      signup,
      logout,
    }
  }, [
    token,
    currentUser.data,
    currentUser.isPending,
    currentUser.isError,
    currentUser.error,
    retryRestore,
    notice,
    login,
    signup,
    logout,
  ])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

/**
 * Read the session.
 *
 * @returns The current {@link AuthContextValue}.
 *
 * @throws Error - When called outside {@link AuthProvider}.
 */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (context === null) {
    throw new Error('useAuth must be used inside AuthProvider')
  }
  return context
}
