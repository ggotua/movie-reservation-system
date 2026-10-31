import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AuthProvider } from '@/features/auth/AuthContext'
import { LoginPage } from '@/features/auth/LoginPage'
import { RequireAuth } from '@/features/auth/RequireAuth'
import { SignupPage } from '@/features/auth/SignupPage'

/** Props of {@link PlaceholderPage}. */
interface PlaceholderPageProps {
  /** The screen name, shown as the page's only content. */
  title: string
}

/**
 * A heading-only stand-in for a screen that later prompts will build.
 *
 * @param props - The screen name.
 * @returns A minimal page with the given title.
 */
function PlaceholderPage({ title }: PlaceholderPageProps) {
  return (
    <main className="mx-auto flex min-h-svh w-full max-w-5xl flex-col justify-center gap-4 px-6">
      <h1 className="text-6">{title}</h1>
    </main>
  )
}

/**
 * Build the app's query client.
 *
 * @returns A client with automatic retries and focus refetches off: every data
 *   screen offers an explicit retry instead (SPEC-7 FR-14), and the auth
 *   session is checked once per page load.
 */
function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
    },
  })
}

/**
 * Render the whole app.
 *
 * @returns The providers and the routed pages.
 */
function App() {
  const [queryClient] = useState(createQueryClient)

  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            <Route path="/" element={<PlaceholderPage title="Showtimes" />} />
            <Route
              path="/movies/:movieId"
              element={<PlaceholderPage title="Movie" />}
            />
            <Route
              path="/showtimes/:showtimeId"
              element={<PlaceholderPage title="Seat map" />}
            />
            <Route
              path="/checkout/:reservationId"
              element={
                <RequireAuth>
                  <PlaceholderPage title="Checkout" />
                </RequireAuth>
              }
            />
            <Route
              path="/reservations"
              element={
                <RequireAuth>
                  <PlaceholderPage title="My reservations" />
                </RequireAuth>
              }
            />
            <Route path="/login" element={<LoginPage />} />
            <Route path="/signup" element={<SignupPage />} />
            <Route
              path="*"
              element={<PlaceholderPage title="Page not found" />}
            />
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  )
}

export default App
