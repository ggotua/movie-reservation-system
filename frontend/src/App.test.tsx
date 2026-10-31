import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from './App'

/**
 * Render the app at a given URL (SPEC-7 Section 5.1, test 38).
 *
 * @param path - The path (plus query) to start from; BrowserRouter reads it
 *   from `window.history`.
 * @returns The Testing Library render result.
 */
function renderAt(path: string) {
  window.history.replaceState({}, '', path)
  return render(<App />)
}

describe('App', () => {
  it('unknown_route_shows_not_found', () => {
    renderAt('/this-page-does-not-exist')

    expect(
      screen.getByRole('heading', { level: 1, name: 'Page not found' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: 'Browse showtimes' }),
    ).toHaveAttribute('href', '/')
  })
})

