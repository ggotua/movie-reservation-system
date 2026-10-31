import { afterAll, describe, expect, it } from 'vitest'

import {
  addDays,
  dayStrip,
  formatDay,
  formatShowtime,
  todayInCinema,
} from './time'

/** The zone the test process started with, restored after the suite. */
const originalTimeZone = process.env.TZ

/**
 * Restore `process.env.TZ` exactly, deleting it when it was originally unset
 * (assigning `undefined` would store the string "undefined").
 */
function restoreTimeZone(): void {
  if (originalTimeZone === undefined) {
    delete process.env.TZ
  } else {
    process.env.TZ = originalTimeZone
  }
}

afterAll(restoreTimeZone)

describe('time', () => {
  it('formatShowtime_uses_cinema_timezone_not_browser_timezone', () => {
    // 20:30 UTC is 00:30 on 31 October in Tbilisi (UTC+4, no DST) and still
    // 30 October in New York; 19:30 UTC stays on 31 October in Tbilisi.
    const lateEveningUtc = '2026-10-30T20:30:00Z'
    const eveningUtc = '2026-10-31T19:30:00Z'

    const browserHours: number[] = []
    const results: { time: string; evening: string; day: string }[] = []

    try {
      // Two zones chosen so the browser's own calendar day differs for the
      // same instant (UTC-4 and UTC+14). Node reads `TZ` when it builds the
      // local-time view of a Date, so setting it here changes the zone the
      // formatters would use if they did not pass `timeZone` explicitly.
      for (const timeZone of ['America/New_York', 'Pacific/Kiritimati']) {
        process.env.TZ = timeZone
        // Proves the switch took effect; without this the two iterations
        // could both run in the machine's default zone and prove nothing.
        expect(Intl.DateTimeFormat().resolvedOptions().timeZone).toBe(timeZone)
        browserHours.push(new Date(lateEveningUtc).getHours())
        results.push({
          time: formatShowtime(lateEveningUtc),
          evening: formatShowtime(eveningUtc),
          day: formatDay(lateEveningUtc),
        })
      }
    } finally {
      restoreTimeZone()
    }

    // The browser really did see two different local times ...
    expect(new Set(browserHours).size).toBe(2)
    // ... while the app's output is identical and always Tbilisi's.
    const expected = [
      { time: '00:30', evening: '23:30', day: 'Saturday 31 October' },
      { time: '00:30', evening: '23:30', day: 'Saturday 31 October' },
    ]
    expect(results).toEqual(expected)
  })

  it('date_helpers_today_in_cinema_timezone_and_day_navigation', () => {
    // 23:30 and 00:30 in Tbilisi fall on different Tbilisi days although both
    // instants are 30 October in UTC.
    expect(todayInCinema(new Date('2026-10-30T19:30:00Z'))).toBe('2026-10-30')
    expect(todayInCinema(new Date('2026-10-30T20:30:00Z'))).toBe('2026-10-31')

    // Month, year and leap-day boundaries, plus a negative offset.
    expect(addDays('2026-10-31', 1)).toBe('2026-11-01')
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01')
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28')
    expect(addDays('2028-02-28', 1)).toBe('2028-02-29')
    expect(addDays('2026-10-31', 0)).toBe('2026-10-31')

    // The day strip starts at the given day and spans the month boundary.
    expect(dayStrip('2026-10-31', 3)).toEqual([
      '2026-10-31',
      '2026-11-01',
      '2026-11-02',
    ])
    expect(dayStrip('2026-10-31', 0)).toEqual([])

    // A malformed day is a programming error, not a silent fallback.
    expect(() => addDays('31/10/2026', 1)).toThrow(/YYYY-MM-DD/)
  })
})
