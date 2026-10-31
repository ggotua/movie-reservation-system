/**
 * The login screen (SPEC-7 Section 2.3, FR-1).
 *
 * A plain controlled form: the token is stored by `AuthContext`, then the user
 * goes to `safeNextPath(next)` so a protected-route redirect returns them to
 * the page they asked for. Invalid field input is caught before any request;
 * a rejected login shows the friendly message for the backend code (FR-8).
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'

import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { safeNextPath } from '@/lib/safeNextPath'

import {
  messageFromError,
  noticeFromState,
  validateCredentials,
} from './authForm'
import type { CredentialFieldErrors } from './authForm'
import { useAuth } from './AuthContext'

/**
 * Render the login form.
 *
 * @returns The page, including the session-ended notice when a redirect
 *   carried one.
 */
export function LoginPage() {
  const { login, notice: sessionNotice } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const next = searchParams.get('next')
  const notice = noticeFromState(location.state) ?? sessionNotice

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fieldErrors, setFieldErrors] = useState<CredentialFieldErrors>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [isPending, setIsPending] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (isPending) {
      return
    }

    const errors = validateCredentials({ email, password })
    setFieldErrors(errors)
    setFormError(null)
    if (errors.email !== undefined || errors.password !== undefined) {
      return
    }

    setIsPending(true)
    try {
      await login({ email: email.trim(), password })
      navigate(safeNextPath(next), { replace: true })
    } catch (error) {
      setFormError(messageFromError(error))
    } finally {
      setIsPending(false)
    }
  }

  const signupHref =
    next === null ? '/signup' : `/signup?next=${encodeURIComponent(next)}`

  return (
    <main className="mx-auto flex min-h-svh w-full max-w-md flex-col justify-center gap-6 px-6 py-10">
      <h1 className="text-6">Log in</h1>

      {notice !== null && (
        <Alert>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}

      <form className="flex flex-col gap-4" noValidate onSubmit={handleSubmit}>
        <div className="flex flex-col gap-2">
          <Label htmlFor="login-email">Email</Label>
          <Input
            id="login-email"
            name="email"
            type="email"
            autoComplete="email"
            className="h-10"
            value={email}
            aria-invalid={fieldErrors.email !== undefined}
            aria-describedby={
              fieldErrors.email === undefined ? undefined : 'login-email-error'
            }
            onChange={(event) => {
              setEmail(event.target.value)
              setFieldErrors((current) => ({ ...current, email: undefined }))
            }}
          />
          {fieldErrors.email !== undefined && (
            <p id="login-email-error" className="text-1 text-destructive">
              {fieldErrors.email}
            </p>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="login-password">Password</Label>
          <Input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            className="h-10"
            value={password}
            aria-invalid={fieldErrors.password !== undefined}
            aria-describedby={
              fieldErrors.password === undefined
                ? undefined
                : 'login-password-error'
            }
            onChange={(event) => {
              setPassword(event.target.value)
              setFieldErrors((current) => ({ ...current, password: undefined }))
            }}
          />
          {fieldErrors.password !== undefined && (
            <p id="login-password-error" className="text-1 text-destructive">
              {fieldErrors.password}
            </p>
          )}
        </div>

        {formError !== null && (
          <Alert variant="destructive">
            <AlertDescription>{formError}</AlertDescription>
          </Alert>
        )}

        <Button
          type="submit"
          className="h-10 px-4"
          disabled={isPending}
          aria-busy={isPending}
        >
          {isPending ? 'Logging in' : 'Log in'}
        </Button>
      </form>

      <p className="text-1 text-muted-foreground">
        New here?{' '}
        <Link className="text-ink underline underline-offset-4" to={signupHref}>
          Sign up
        </Link>
        .
      </p>
    </main>
  )
}
