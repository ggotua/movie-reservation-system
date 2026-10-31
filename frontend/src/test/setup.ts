import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterAll, afterEach, beforeAll } from 'vitest'
import { server } from './server'

beforeAll(() => {
  // Any request without a handler is a test bug, not a network accident.
  server.listen({ onUnhandledFrame: 'error' })
})

afterEach(() => {
  // Testing Library cannot auto-register cleanup without Vitest globals.
  cleanup()
  server.resetHandlers()
})

afterAll(() => {
  server.close()
})
