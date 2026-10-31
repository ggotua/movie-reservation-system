/**
 * Runtime configuration read from `import.meta.env`.
 *
 * This is the only module that reads `VITE_*` variables (SPEC-7 section 10).
 * Every value here is compiled into the public bundle, so no secret may ever
 * be placed in one.
 */

/** Backend API default; matches the CORS origins the backend allows in dev. */
const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000'

/** Default display currency (SPEC-7 D5). */
const DEFAULT_CURRENCY = 'GEL'

/** Default cinema time zone; mirrors the backend's CINEMA_TIMEZONE default. */
const DEFAULT_CINEMA_TIMEZONE = 'Asia/Tbilisi'

/**
 * Reads an optional environment value.
 *
 * @param value - Raw value from `import.meta.env`, possibly `undefined`.
 * @param fallback - Used when the variable is missing or only whitespace.
 * @returns The trimmed value, or `fallback`.
 */
function readEnv(value: string | undefined, fallback: string): string {
  const trimmed = value?.trim()
  return trimmed === undefined || trimmed === '' ? fallback : trimmed
}

/** Resolved application configuration, computed once at module load. */
export const config = {
  /** Base URL of the backend API, e.g. `http://127.0.0.1:8000`. */
  apiBaseUrl: readEnv(import.meta.env.VITE_API_BASE_URL, DEFAULT_API_BASE_URL),
  /** ISO 4217 currency code used to format money, e.g. `GEL`. */
  currency: readEnv(import.meta.env.VITE_CURRENCY, DEFAULT_CURRENCY),
  /** IANA time zone every showtime is displayed in, e.g. `Asia/Tbilisi`. */
  cinemaTimezone: readEnv(
    import.meta.env.VITE_CINEMA_TIMEZONE,
    DEFAULT_CINEMA_TIMEZONE,
  ),
} as const

/** Shape of the resolved application configuration. */
export type AppConfig = typeof config
