import type { RequestHandler } from 'msw'
import { setupServer } from 'msw/node'

/**
 * MSW request handlers shared by every test. Feature suites add their own with
 * `server.use(...)`; nothing needs mocking yet.
 */
export const handlers: RequestHandler[] = []

/**
 * Mock server used by `src/test/setup.ts`: started before the suite, reset
 * after each test, closed at the end.
 */
export const server = setupServer(...handlers)
