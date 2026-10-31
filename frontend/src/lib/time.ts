/**
 * Date and time helpers, always in the cinema's time zone (SPEC-7 FR-10, D5).
 *
 * The backend stores and returns UTC instants but interprets calendar days in
 * `CINEMA_TIMEZONE`. Every formatter here passes `timeZone` explicitly, so the
 * browser's own zone never changes what a user sees; the default day is also
 * computed in that zone. `lib/config.ts` is the only reader of the environment
 * variable.
 */

import { config } from './config'

/** Locale pinned by the app: the UI is English only (SPEC-7 D4). */
const LOCALE = 'en-GB'

/** The cinema's IANA time zone, e.g. `Asia/Tbilisi`. */
const TIME_ZONE = config.cinemaTimezone

/** Accepts an ISO string (as the API sends) or an already constructed date. */
type DateInput = string | Date

/**
 * Turn a `DateInput` into a `Date`.
 *
 * @param value - An ISO 8601 string or a `Date`.
 * @returns The equivalent `Date`.
 *
 * Non-obvious: a `YYYY-MM-DD` string is read by JavaScript as UTC midnight,
 * which still falls on the same calendar day in the cinema zone for every zone
 * the app supports; formatting then applies the cinema zone explicitly.
 */
function toDate(value: DateInput): Date {
  return typeof value === 'string' ? new Date(value) : value
}

/**
 * Format a showtime's time of day in the cinema time zone.
 *
 * @param value - The showtime instant (`starts_at`) as an ISO string or `Date`.
 * @returns `HH:mm`, 24-hour, e.g. `18:00`. An instant late in the UTC evening
 *   correctly rolls over to the next local day (`20:30Z` -> `00:30`).
 */
export function formatShowtime(value: DateInput): string {
  return new Intl.DateTimeFormat(LOCALE, {
    timeZone: TIME_ZONE,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(toDate(value))
}

/**
 * Format a calendar day in the cinema time zone, as the browse page title.
 *
 * @param value - A day (`YYYY-MM-DD`) or any instant belonging to that day.
 * @returns The full weekday, day and month, e.g. `Saturday 31 October`.
 */
export function formatDay(value: DateInput): string {
  return new Intl.DateTimeFormat(LOCALE, {
    timeZone: TIME_ZONE,
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  }).format(toDate(value))
}

/**
 * Today's calendar day in the cinema time zone, as the API expects it.
 *
 * @param now - The instant to evaluate; defaults to the current time. Tests
 *   pass a fixed instant instead of mocking the clock.
 * @returns `YYYY-MM-DD` for the day that is current in `VITE_CINEMA_TIMEZONE`,
 *   which may differ from the browser's day around midnight.
 */
export function todayInCinema(now: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat(LOCALE, {
    timeZone: TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now)
  const year = parts.find((part) => part.type === 'year')?.value
  const month = parts.find((part) => part.type === 'month')?.value
  const day = parts.find((part) => part.type === 'day')?.value
  if (year === undefined || month === undefined || day === undefined) {
    throw new Error(`Could not resolve the date in ${TIME_ZONE}`)
  }
  return `${year}-${month}-${day}`
}

/** Matches a calendar day in the API's `YYYY-MM-DD` form. */
const DAY_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/

/**
 * Add a number of calendar days to a `YYYY-MM-DD` day.
 *
 * The arithmetic runs on UTC fields only, so daylight-saving changes and the
 * browser's time zone cannot shorten or lengthen a day.
 *
 * @param day - A calendar day in `YYYY-MM-DD` form.
 * @param days - Days to add; negative values subtract.
 * @returns The resulting `YYYY-MM-DD` day.
 *
 * @throws Error - If `day` is not in `YYYY-MM-DD` form.
 */
export function addDays(day: string, days: number): string {
  const match = DAY_PATTERN.exec(day)
  if (match === null) {
    throw new Error(`Expected a YYYY-MM-DD day, received "${day}"`)
  }
  const date = new Date(
    Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])),
  )
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

/**
 * Build the browse page's day strip: consecutive days starting at `today`.
 *
 * @param today - The first day, `YYYY-MM-DD` (usually `todayInCinema()`).
 * @param count - How many days the strip holds, `today` included. Zero or a
 *   negative value yields an empty strip.
 * @returns `count` consecutive `YYYY-MM-DD` days, e.g. `dayStrip('2026-10-31',
 *   14)` returns 31 October through 13 November.
 *
 * @throws Error - If `today` is not in `YYYY-MM-DD` form.
 */
export function dayStrip(today: string, count: number): string[] {
  const total = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0
  return Array.from({ length: total }, (_, index) => addDays(today, index))
}
