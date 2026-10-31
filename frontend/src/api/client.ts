/**
 * The only module that talks HTTP (SPEC-7 Section 10, DRY check).
 *
 * `request` prefixes `VITE_API_BASE_URL`, sends and accepts JSON, attaches the
 * bearer token when one is stored, and turns every failure into an
 * {@link ApiError} carrying the backend's `status`, `code` and `message`. On a
 * `401 UNAUTHENTICATED` it also clears the stored token and notifies the
 * registered callback, which the auth layer uses to redirect to `/login`
 * (SPEC-7 FR-11).
 *
 * A network failure (the server is unreachable) has no HTTP status and no
 * backend code, so it becomes status `0` and code `NETWORK_ERROR`.
 */

import { config } from '@/lib/config'
import { ERROR_MESSAGES, GENERIC_ERROR_MESSAGE } from '@/lib/errorMessages'
import { clearToken, getToken } from '@/lib/tokenStorage'

import type { ApiErrorBody } from './types'

/** The code reported when `fetch` itself fails, i.e. no response arrived. */
export const NETWORK_ERROR = 'NETWORK_ERROR'

/** The fallback code when a failure body is not the standard shape. */
const HTTP_ERROR = 'HTTP_ERROR'

/** The base URL with any trailing slash removed, so paths join predictably. */
const API_BASE_URL = config.apiBaseUrl.replace(/\/+$/, '')

/** A failed API call: the HTTP status, the backend code and a message. */
export class ApiError extends Error {
  /** The HTTP status, or `0` for a network failure. */
  readonly status: number

  /** The backend's `error.code`, or `NETWORK_ERROR` / `HTTP_ERROR`. */
  readonly code: string

  /**
   * @param status - The HTTP status, or `0` when no response arrived.
   * @param code - The backend's stable error code.
   * @param message - The backend's message, safe to show to a user.
   */
  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

/** A callback invoked after a `401 UNAUTHENTICATED`, e.g. to redirect. */
export type UnauthorizedHandler = () => void

/** Registered callbacks; a `Set` so the same callback is never called twice. */
const unauthorizedHandlers = new Set<UnauthorizedHandler>()

/**
 * Register a callback for a `401 UNAUTHENTICATED` response.
 *
 * @param handler - Called after the token has been cleared. It must not throw;
 *   an exception is swallowed so the original {@link ApiError} still reaches
 *   the caller.
 * @returns An unsubscribe function, so a component can unregister on unmount.
 */
export function registerUnauthorizedHandler(
  handler: UnauthorizedHandler,
): () => void {
  unauthorizedHandlers.add(handler)
  return () => {
    unauthorizedHandlers.delete(handler)
  }
}

/** HTTP methods the app uses. */
type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE'

/** Optional per-call settings for {@link request}. */
export interface RequestOptions {
  /** Defaults to `GET`. */
  method?: HttpMethod
  /** Sent as JSON when present; omitted entirely for a bodyless request. */
  body?: unknown
  /** Allows a caller to cancel the request. */
  signal?: AbortSignal
}

/** Query parameter values `buildQuery` accepts. */
type QueryValue = string | number | boolean | null | undefined

/**
 * Build a query string from defined values.
 *
 * @param params - Values keyed by their API parameter name (`snake_case`, e.g.
 *   `movie_id`). `undefined`, `null` and `''` are omitted, so an unset filter
 *   is simply not sent.
 * @returns `?key=value&...`, or `''` when nothing is defined. Values are
 *   URL-encoded by `URLSearchParams`.
 */
export function buildQuery(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') {
      continue
    }
    search.set(key, String(value))
  }
  const query = search.toString()
  return query === '' ? '' : `?${query}`
}

/**
 * Narrow a parsed JSON value to the standard error body.
 *
 * @param value - The parsed response body.
 * @returns `true` when `value.error.code` and `value.error.message` are strings.
 */
function isApiErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const error = (value as { error?: unknown }).error
  if (typeof error !== 'object' || error === null) {
    return false
  }
  const detail = error as { code?: unknown; message?: unknown }
  return typeof detail.code === 'string' && typeof detail.message === 'string'
}

/**
 * Parse a response body as JSON without throwing.
 *
 * @param text - The raw response body.
 * @returns The parsed value, or `undefined` when the body is empty or invalid.
 */
function parseJson(text: string): unknown {
  if (text === '') {
    return undefined
  }
  try {
    return JSON.parse(text) as unknown
  } catch {
    return undefined
  }
}

/**
 * Turn a non-2xx response into an {@link ApiError} and run the 401 side effect.
 *
 * @param status - The HTTP status.
 * @param text - The raw response body.
 * @returns The error to throw.
 */
function toApiError(status: number, text: string): ApiError {
  const parsed = parseJson(text)
  const detail = isApiErrorBody(parsed) ? parsed.error : null
  const code = detail?.code ?? HTTP_ERROR
  const message = detail?.message ?? `Request failed with status ${status}.`

  if (status === 401 && code === 'UNAUTHENTICATED') {
    clearToken()
    for (const handler of [...unauthorizedHandlers]) {
      try {
        handler()
      } catch {
        // A failing listener must not hide the original error.
      }
    }
  }

  return new ApiError(status, code, message)
}

/**
 * Perform one API call.
 *
 * @param path - The path below `VITE_API_BASE_URL`, starting with `/`, plus the
 *   query string from {@link buildQuery}, e.g. `/showtimes?date=2026-10-31`.
 * @param options - Method, optional JSON body and optional abort signal.
 * @returns The parsed JSON response; `undefined` (typed as `T`) for a `204`.
 *
 * @throws ApiError - On any non-2xx response, or with status `0` and code
 *   `NETWORK_ERROR` when the server cannot be reached. An abort is re-thrown
 *   unchanged, because a cancelled request is not a failure to report.
 */
export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = 'GET', body, signal } = options
  const headers: Record<string, string> = { Accept: 'application/json' }

  const token = getToken()
  if (token !== null) {
    headers.Authorization = `Bearer ${token}`
  }

  let payload: string | undefined
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      body: payload,
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw error
    }
    throw new ApiError(
      0,
      NETWORK_ERROR,
      ERROR_MESSAGES[NETWORK_ERROR] ?? GENERIC_ERROR_MESSAGE,
    )
  }

  const text = await response.text()
  if (!response.ok) {
    throw toApiError(response.status, text)
  }
  if (text === '') {
    return undefined as T
  }
  return JSON.parse(text) as T
}
