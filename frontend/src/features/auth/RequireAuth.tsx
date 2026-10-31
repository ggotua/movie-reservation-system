/**
 * Gate a route behind a session (SPEC-7 FR-1, Section 2.3).
 *
 * An anonymous visitor is sent to `/login?next=<the path they asked for>`, and
 * `LoginPage` returns them there after a successful login. A stored token is
 * checked with `GET /auth/me` first; while that call runs the screen waits, and
 * a failure other than 401 shows the friendly message with a retry action
 * (FR-14). A 401 clears the token in `api/client.ts`, which turns the visitor
 * anonymous and lets the redirect above take over.
 */

import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'

import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { messageFromError } from './authForm'
import { useAuth } from './AuthContext'

/** Props of {@link RequireAuth}. */
export interface RequireAuthProps {
  /** The protected screen, rendered only for a restored session. */
  children: ReactNode
}

/**
 * Render a screen only when the visitor has a session.
 *
 * @param props - The protected screen.
 * @returns The screen, a redirect to login, a loading placeholder, or an error
 *   with a retry action.
 */
export function RequireAuth({ children }: RequireAuthProps) {
  const { token, isRestoring, restoreError, retryRestore } = useAuth()
  const location = useLocation()

  if (token === null) {
    const next = `${location.pathname}${location.search}${location.hash}`
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />
  }

  if (isRestoring) {
    return (
      <main className="mx-auto flex min-h-svh w-full max-w-md flex-col justify-center px-6">
        <Skeleton className="h-10 w-full" />
      </main>
    )
  }

  if (restoreError !== null) {
    return (
      <main className="mx-auto flex min-h-svh w-full max-w-md flex-col justify-center gap-4 px-6">
        <Alert variant="destructive">
          <AlertDescription>{messageFromError(restoreError)}</AlertDescription>
        </Alert>
        <Button
          type="button"
          variant="outline"
          className="h-10 px-4"
          onClick={retryRestore}
        >
          Try again
        </Button>
      </main>
    )
  }

  return <>{children}</>
}
