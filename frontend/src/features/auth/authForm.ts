/**
 * Shared form rules for the login and signup pages (SPEC-7 prompt 4).
 *
 * The client validation mirrors the server's policy: an email that looks like
 * an address, and a password of at least 8 characters and at most 72 bytes
 * (bcrypt's limit, measured as UTF-8 bytes rather than characters). Keeping the
 * rules here means both forms reject exactly the same input.
 */

import { ApiError } from '@/api/client'
import { messageFor } from '@/lib/errorMessages'

/** The shortest password the backend accepts, in characters. */
export const MIN_PASSWORD_CHARACTERS = 8

/** The longest password the backend accepts, in UTF-8 bytes. */
export const MAX_PASSWORD_BYTES = 72

/** Per-field messages for the credential form; a missing key means valid. */
export interface CredentialFieldErrors {
  email?: string
  password?: string
}

/** The values a credential form validates; the same shape the API takes. */
export interface CredentialValues {
  email: string
  password: string
}

/** A deliberately loose address check: one `@`, something on both sides, a dot. */
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

/** Counts the UTF-8 bytes of a string, matching what the server hashes. */
function countBytes(value: string): number {
  return new TextEncoder().encode(value).length
}

/**
 * Validate an email address typed into the form.
 *
 * @param value - The raw input value; surrounding whitespace is ignored.
 * @returns A message when the value is empty or does not look like an address,
 *   otherwise `null`.
 */
export function validateEmail(value: string): string | null {
  const email = value.trim()
  if (email === '') {
    return 'Enter your email address.'
  }
  return EMAIL_PATTERN.test(email) ? null : 'Enter a valid email address.'
}

/**
 * Validate a password typed into the form.
 *
 * @param value - The raw password; never trimmed, because spaces are allowed
 *   inside a password.
 * @returns A message when the password is empty, shorter than
 *   {@link MIN_PASSWORD_CHARACTERS} characters or longer than
 *   {@link MAX_PASSWORD_BYTES} UTF-8 bytes, otherwise `null`.
 *
 * Non-obvious: the upper limit counts bytes, so a password of fewer than 72
 * characters can still be rejected when it contains multi-byte characters.
 */
export function validatePassword(value: string): string | null {
  if (value === '') {
    return 'Enter your password.'
  }
  if (value.length < MIN_PASSWORD_CHARACTERS) {
    return `Password must be at least ${MIN_PASSWORD_CHARACTERS} characters.`
  }
  if (countBytes(value) > MAX_PASSWORD_BYTES) {
    return `Password must be at most ${MAX_PASSWORD_BYTES} bytes.`
  }
  return null
}

/**
 * Validate both credential fields at once.
 *
 * @param values - The current form values.
 * @returns One entry per invalid field; an empty object when both are valid.
 */
export function validateCredentials(
  values: CredentialValues,
): CredentialFieldErrors {
  const errors: CredentialFieldErrors = {}
  const email = validateEmail(values.email)
  const password = validatePassword(values.password)
  if (email !== null) {
    errors.email = email
  }
  if (password !== null) {
    errors.password = password
  }
  return errors
}

/**
 * Turn a caught failure into the message to show above a form.
 *
 * @param error - Whatever the login/signup call threw.
 * @returns The friendly text for the backend code (SPEC-7 FR-8), or the
 *   generic message when the failure is not an `ApiError`.
 */
export function messageFromError(error: unknown): string {
  if (error instanceof ApiError) {
    return messageFor(error.code, error.message)
  }
  return messageFor(null)
}

/**
 * Read the short notice a redirect places in the router location state.
 *
 * @param state - `location.state`, whose type React Router leaves as `unknown`.
 * @returns The notice string, or `null` when there is none.
 */
export function noticeFromState(state: unknown): string | null {
  if (typeof state !== 'object' || state === null) {
    return null
  }
  const notice = (state as { notice?: unknown }).notice
  return typeof notice === 'string' && notice !== '' ? notice : null
}
