/**
 * Typed auth endpoints (SPEC-7 Section 2.4, SPEC-2 Section 2.2).
 *
 * Thin wrappers over `api/client.ts`: they name the request and response types
 * and nothing else. Storing the token after `login` is the auth layer's job,
 * not this module's.
 */

import { request } from './client'

import type { Token, User } from './types'

/** The body of signup and login. */
export interface Credentials {
  email: string
  password: string
}

/**
 * Create an account (`POST /auth/signup`).
 *
 * @param credentials - The email and password to register.
 * @returns The new account. New accounts always have role `user`.
 *
 * @throws ApiError - `EMAIL_ALREADY_REGISTERED` (409) or
 *   `VALIDATION_ERROR` (422) when the password is outside the policy.
 */
export function signup(credentials: Credentials): Promise<User> {
  return request<User>('/auth/signup', { method: 'POST', body: credentials })
}

/**
 * Log in (`POST /auth/login`).
 *
 * @param credentials - The email and password to check.
 * @returns The bearer token and its lifetime.
 *
 * @throws ApiError - `INVALID_CREDENTIALS` (401) for a wrong email or password.
 */
export function login(credentials: Credentials): Promise<Token> {
  return request<Token>('/auth/login', { method: 'POST', body: credentials })
}

/**
 * Read the current account (`GET /auth/me`).
 *
 * @returns The account belonging to the stored bearer token.
 *
 * @throws ApiError - `UNAUTHENTICATED` (401) when the token is missing,
 *   malformed or expired.
 */
export function getCurrentUser(): Promise<User> {
  return request<User>('/auth/me')
}
