import { describe, expect, it } from 'vitest'

import { formatRemaining, isExpired, remainingMs } from './countdown'

/** A hold that expires exactly at 10:00 UTC. */
const EXPIRES_AT = '2026-10-31T10:00:00Z'
const EXPIRY_MS = Date.parse(EXPIRES_AT)

describe('countdown', () => {
  it('countdown_remaining_and_expiry_boundary', () => {
    // One second and one millisecond before expiry: still a live hold.
    expect(remainingMs(EXPIRES_AT, EXPIRY_MS - 1000)).toBe(1000)
    expect(remainingMs(EXPIRES_AT, EXPIRY_MS - 1)).toBe(1)
    expect(isExpired(EXPIRES_AT, EXPIRY_MS - 1)).toBe(false)

    // At the expiry instant and after it: expired, clamped at zero.
    expect(remainingMs(EXPIRES_AT, EXPIRY_MS)).toBe(0)
    expect(remainingMs(EXPIRES_AT, EXPIRY_MS + 1000)).toBe(0)
    expect(isExpired(EXPIRES_AT, EXPIRY_MS)).toBe(true)
    expect(isExpired(EXPIRES_AT, EXPIRY_MS + 1000)).toBe(true)

    // A Date is accepted exactly like epoch milliseconds.
    expect(remainingMs(EXPIRES_AT, new Date(EXPIRY_MS - 90_000))).toBe(90_000)
    expect(isExpired(EXPIRES_AT, new Date(EXPIRY_MS))).toBe(true)

    // An unparseable expiry is treated as expired rather than as "no limit".
    expect(remainingMs('not-a-date', EXPIRY_MS)).toBe(0)

    // Display: ten minutes, rounding a partial second up, and zero.
    expect(formatRemaining(600_000)).toBe('10:00')
    expect(formatRemaining(90_000)).toBe('01:30')
    expect(formatRemaining(1)).toBe('00:01')
    expect(formatRemaining(0)).toBe('00:00')
    expect(formatRemaining(-500)).toBe('00:00')
  })
})
