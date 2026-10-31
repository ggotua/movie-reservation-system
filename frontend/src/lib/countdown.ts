/**
 * Hold countdown arithmetic (SPEC-7 FR-6).
 *
 * The countdown is derived from the server's `expires_at` and the local clock;
 * it is display only. When it reaches zero the checkout page refetches the
 * reservation, and the server's status decides what happens, so a wrong client
 * clock can never extend a hold.
 */

/** Accepts the current instant as a `Date` or as epoch milliseconds. */
type Instant = Date | number

/**
 * Read an instant as epoch milliseconds.
 *
 * @param now - A `Date` or epoch milliseconds.
 * @returns The same instant in epoch milliseconds.
 */
function toMillis(now: Instant): number {
  return typeof now === 'number' ? now : now.getTime()
}

/**
 * Milliseconds left until a hold expires.
 *
 * @param expiresAt - The server's `expires_at`, an ISO 8601 string.
 * @param now - The current instant; defaults to the current time.
 * @returns The remaining milliseconds, clamped at `0` (an expired or
 *   unparseable value is treated as already expired).
 */
export function remainingMs(expiresAt: string, now: Instant = Date.now()): number {
  const expiry = Date.parse(expiresAt)
  if (Number.isNaN(expiry)) {
    return 0
  }
  return Math.max(0, expiry - toMillis(now))
}

/**
 * Format a remaining duration as `mm:ss`.
 *
 * @param ms - Milliseconds left, as returned by {@link remainingMs}.
 * @returns Zero-padded minutes and seconds, e.g. `10:00` or `00:05`. A
 *   positive remainder is rounded *up* so the last second is never shown as
 *   `00:00` while the hold is still valid; `0` prints `00:00`. Durations of an
 *   hour or more keep counting in minutes (`75:30`).
 */
export function formatRemaining(ms: number): string {
  const totalSeconds = Math.ceil(Math.max(0, ms) / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

/**
 * Whether a hold has reached its expiry.
 *
 * @param expiresAt - The server's `expires_at`, an ISO 8601 string.
 * @param now - The current instant; defaults to the current time.
 * @returns `true` when no time is left. The exact expiry instant counts as
 *   expired, so a hold is never shown as active at `expires_at`.
 */
export function isExpired(expiresAt: string, now: Instant = Date.now()): boolean {
  return remainingMs(expiresAt, now) === 0
}
