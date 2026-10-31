import { describe, expect, it } from 'vitest'

import { safeNextPath } from './safeNextPath'

describe('safeNextPath', () => {
  it('accepts a same-origin relative path', () => {
    expect(safeNextPath('/showtimes/1')).toBe('/showtimes/1')
    expect(safeNextPath('/checkout/5?from=login')).toBe('/checkout/5?from=login')
    expect(safeNextPath('/reservations')).toBe('/reservations')
  })

  it('falls back to "/" for absolute, protocol-relative and script URLs', () => {
    expect(safeNextPath('https://evil.example')).toBe('/')
    expect(safeNextPath('//evil.example')).toBe('/')
    expect(safeNextPath('javascript:alert(1)')).toBe('/')
    expect(safeNextPath('/\\evil.example')).toBe('/')
  })

  it('falls back to "/" for an empty or missing value', () => {
    expect(safeNextPath('')).toBe('/')
    expect(safeNextPath(null)).toBe('/')
    expect(safeNextPath(undefined)).toBe('/')
    expect(safeNextPath(' /showtimes/1')).toBe('/')
  })
})
