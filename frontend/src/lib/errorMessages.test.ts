import { readFileSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  ERROR_MESSAGES,
  GENERIC_ERROR_MESSAGE,
  messageFor,
} from './errorMessages'

/**
 * Every backend error code the app can receive, transcribed from SPEC-2 to
 * SPEC-6 (the source of truth is `backend/src/core/errors.py` and the
 * `ApiError(...)` calls of each module). Keeping the list explicit is the
 * point: adding a backend code without a friendly message here must fail.
 */
const SPEC_ERROR_CODES = [
  // SPEC-2 (auth).
  'UNAUTHENTICATED',
  'FORBIDDEN',
  'INVALID_CREDENTIALS',
  'EMAIL_ALREADY_REGISTERED',
  'USER_NOT_FOUND',
  // SPEC-3 (catalog).
  'GENRE_NOT_FOUND',
  'MOVIE_NOT_FOUND',
  'GENRE_ALREADY_EXISTS',
  'GENRE_IN_USE',
  'MOVIE_HAS_SHOWTIMES',
  'SCREEN_NOT_FOUND',
  'SCREEN_NAME_TAKEN',
  'SHOWTIME_NOT_FOUND',
  'SHOWTIME_IN_PAST',
  'SHOWTIME_OVERLAP',
  'SHOWTIME_STARTED',
  'SHOWTIME_HAS_RESERVATIONS',
  // SPEC-4 and SPEC-5 (seats, holds, list, cancel).
  'SEAT_NOT_FOUND',
  'SEAT_UNAVAILABLE',
  'ACTIVE_HOLD_EXISTS',
  'TOO_MANY_SEATS',
  'RESERVATION_NOT_FOUND',
  'RESERVATION_EXPIRED',
  'RESERVATION_NOT_HELD',
  'RESERVATION_NOT_CONFIRMED',
  // SPEC-6 (reporting).
  'INVALID_DATE_RANGE',
  // Shared handlers in `backend/src/main.py`.
  'VALIDATION_ERROR',
  'INTERNAL_ERROR',
  'NOT_FOUND',
  'METHOD_NOT_ALLOWED',
  'HTTP_ERROR',
] as const

/** `backend/src`, relative to the `frontend` directory `npm test` runs in. */
const BACKEND_SRC = resolve(process.cwd(), '..', 'backend', 'src')

/**
 * List every Python file below a directory.
 *
 * @param directory - Absolute path to walk.
 * @returns Absolute paths of all `.py` files.
 */
function pythonFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    return entry.isDirectory() ? pythonFiles(path) : path.endsWith('.py') ? [path] : []
  })
}

/**
 * Read the error codes out of the backend sources.
 *
 * Four shapes cover every place a code is defined: a literal passed to
 * `ApiError(...)`, a literal passed to `error_body(...)`, a module constant
 * whose name equals its value (`GENRE_NOT_FOUND = "GENRE_NOT_FOUND"`), and the
 * entries of `main.py`'s HTTP exception map.
 *
 * @returns Every code found, without duplicates.
 */
function collectBackendErrorCodes(): Set<string> {
  const codes = new Set<string>()
  for (const file of pythonFiles(BACKEND_SRC)) {
    const source = readFileSync(file, 'utf8')
    for (const match of source.matchAll(/ApiError\(\s*"([A-Z][A-Z0-9_]*)"/g)) {
      codes.add(match[1])
    }
    for (const match of source.matchAll(/error_body\(\s*"([A-Z][A-Z0-9_]*)"/g)) {
      codes.add(match[1])
    }
    for (const match of source.matchAll(
      /^([A-Z][A-Z0-9_]+)\s*=\s*"([A-Z][A-Z0-9_]+)"\s*$/gm,
    )) {
      if (match[1] === match[2]) {
        codes.add(match[2])
      }
    }
  }

  const main = readFileSync(join(BACKEND_SRC, 'main.py'), 'utf8')
  for (const match of main.matchAll(/\d{3}:\s*\(\s*"([A-Z][A-Z0-9_]+)"/g)) {
    codes.add(match[1])
  }
  for (const match of main.matchAll(
    /_HTTP_EXCEPTION_FALLBACK[^=]*=\s*\(\s*"([A-Z][A-Z0-9_]+)"/g,
  )) {
    codes.add(match[1])
  }

  return codes
}

describe('errorMessages', () => {
  it('errorMessages_cover_every_backend_error_code', () => {
    // The transcribed list itself must be complete and duplicate-free ...
    expect(new Set(SPEC_ERROR_CODES).size).toBe(SPEC_ERROR_CODES.length)
    const backendCodes = collectBackendErrorCodes()
    expect([...backendCodes].sort()).toEqual([...SPEC_ERROR_CODES].sort())

    // ... and every code must have a non-empty friendly message.
    for (const code of SPEC_ERROR_CODES) {
      expect(ERROR_MESSAGES[code], `missing message for ${code}`).toBeTruthy()
      expect(ERROR_MESSAGES[code].trim(), `blank message for ${code}`).not.toBe('')
    }

    // A code that the backend gains later but the map lacks fails the test
    // above through the set comparison; the client-only code is mapped too.
    expect(ERROR_MESSAGES.NETWORK_ERROR).toBeTruthy()
  })

  it('known codes use the friendly message regardless of server text', () => {
    expect(messageFor('MOVIE_NOT_FOUND', 'Movie 999 does not exist.')).toBe(
      ERROR_MESSAGES.MOVIE_NOT_FOUND,
    )
  })

  it('unknown codes fall back to a generic message plus the server text', () => {
    expect(messageFor('SOMETHING_NEW')).toBe(GENERIC_ERROR_MESSAGE)
    expect(messageFor('SOMETHING_NEW', '  raw detail  ')).toBe(
      `${GENERIC_ERROR_MESSAGE} raw detail`,
    )
    expect(messageFor(null)).toBe(GENERIC_ERROR_MESSAGE)
    expect(messageFor(undefined, '   ')).toBe(GENERIC_ERROR_MESSAGE)
  })
})
