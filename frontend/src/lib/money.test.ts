import { describe, expect, it } from 'vitest'

import { config } from './config'
import { formatMoney } from './money'

/**
 * `Intl` separates the currency marker from the amount with a narrow
 * no-break space; the tests normalise it so the expectation stays readable.
 */
function plain(cents: number): string {
  return formatMoney(cents).replace(/\u00a0/g, ' ')
}

describe('formatMoney', () => {
  it('formatMoney_formats_integer_cents_without_float_errors', () => {
    const currency = config.currency

    // 0, 1 and 99 cents: the lower boundary values of the cents field.
    expect(plain(0)).toBe(`${currency} 0.00`)
    expect(plain(1)).toBe(`${currency} 0.01`)
    expect(plain(99)).toBe(`${currency} 0.99`)
    // Exactly one unit, and a non-round amount above it.
    expect(plain(100)).toBe(`${currency} 1.00`)
    expect(plain(12345)).toBe(`${currency} 123.45`)
    // The API's upper price bound, and a large total: grouping plus two
    // decimals, with no float artifact such as 1234567.8899999999.
    expect(plain(100000000)).toBe(`${currency} 1,000,000.00`)
    expect(plain(123456789)).toBe(`${currency} 1,234,567.89`)
    expect(formatMoney(1)).not.toContain('0.009999')
  })
})
