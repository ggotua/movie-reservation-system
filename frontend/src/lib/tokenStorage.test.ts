import { afterEach, describe, expect, it, vi } from 'vitest'

import { clearToken, getToken, setToken } from './tokenStorage'

describe('tokenStorage', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    globalThis.localStorage.clear()
  })

  it('tokenStorage_survives_storage_errors_and_clears', () => {
    // Round trip through the one key.
    expect(getToken()).toBeNull()
    setToken('test.jwt.token')
    expect(getToken()).toBe('test.jwt.token')
    clearToken()
    expect(getToken()).toBeNull()

    // Reading an unavailable storage behaves like "not logged in".
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage denied')
    })
    expect(getToken()).toBeNull()

    // Writing and clearing must not throw, so a blocked storage cannot break
    // login or logout.
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage denied')
    })
    expect(() => setToken('another.token')).not.toThrow()

    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('storage denied')
    })
    expect(() => clearToken()).not.toThrow()
  })
})
