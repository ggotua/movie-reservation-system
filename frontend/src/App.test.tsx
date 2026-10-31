import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import App from './App'

describe('App', () => {
  it('renders', () => {
    render(<App />)

    expect(
      screen.getByRole('heading', { level: 1, name: 'Showtimes' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Hold seats' }),
    ).toBeInTheDocument()
  })
})
