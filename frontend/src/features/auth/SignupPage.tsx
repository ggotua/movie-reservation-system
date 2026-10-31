/**
 * The signup screen (SPEC-7 Section 2.3, FR-1).
 *
 * A plain controlled form. `AuthContext.signup` creates the account and then
 * logs in with the same credentials, because the signup endpoint returns the
 * account rather than a token; the page then goes to `safeNextPath(next)`.
 * Field rules mirror the server (SPEC-7 prompt 4); a duplicate email is shown
 * as the friendly message for `EMAIL_ALREADY_REGISTERED` (FR-8).
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { safeNextPath } from '@/lib/safeNextPath'

import { messageFromError, validateCredentials } from './authForm'
import type { CredentialFieldErrors } from './authForm'
import { useAuth } from './AuthContext'

/**
 * Render the signup form.
 *
 * @returns The page.
 */
export function SignupPage() {
  const { signup } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const next = searchParams.get('next')

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
      await signup({ email: email.trim(), password })
      navigate(safeNextPath(next), { replace: true })
    } catch (error) {
      setFormError(messageFromError(error))
    } finally {
      setIsPending(false)
    }
  }

  const loginHref =
    next === null ? '/login' : `/login?next=${encodeURIComponent(next)}`

  return (
    <main className="mx-auto flex min-h-svh w-full max-w-md flex-col justify-center gap-6 px-6 py-10">
      <h1 className="text-6">Sign up</h1>

      <form className="flex flex-col gap-4" noValidate onSubmit={handleSubmit}>
        <div className="flex flex-col gap-2">
          <Label htmlFor="signup-email">Email</Label>
          <Input
            id="signup-email"
            name="email"
            type="email"
            autoComplete="email"
            className="h-10"
            value={email}
            aria-invalid={fieldErrors.email !== undefined}
            aria-describedby={
              fieldErrors.email === undefined ? undefined : 'signup-email-error'
            }
            onChange={(event) => {
              setEmail(event.target.value)
              setFieldErrors((current) => ({ ...current, email: undefined }))
            }}
          />
          {fieldErrors.email !== undefined && (
            <p id="signup-email-error" className="text-1 text-destructive">
              {fieldErrors.email}
            </p>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="signup-password">Password</Label>
          <Input
            id="signup-password"
            name="password"
            type="password"
            autoComplete="new-password"
            className="h-10"
            value={password}
            aria-invalid={fieldErrors.password !== undefined}
            aria-describedby={
              fieldErrors.password === undefined
                ? undefined
                : 'signup-password-error'
            }
            onChange={(event) => {
              setPassword(event.target.value)
              setFieldErrors((current) => ({ ...current, password: undefined }))
            }}
          />
          {fieldErrors.password !== undefined && (
            <p id="signup-password-error" className="text-1 text-destructive">
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
          {isPending ? 'Signing up' : 'Sign up'}
        </Button>
      </form>

      <p className="text-1 text-muted-foreground">
        Already have an account?{' '}
        <Link className="text-ink underline underline-offset-4" to={loginHref}>
          Log in
        </Link>
        .
      </p>
    </main>
  )
}
