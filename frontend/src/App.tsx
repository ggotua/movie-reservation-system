import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AppShell } from '@/AppShell'
import { NotFoundPage } from '@/NotFoundPage'
import { AuthProvider } from '@/features/auth/AuthContext'
import { LoginPage } from '@/features/auth/LoginPage'
import { RequireAuth } from '@/features/auth/RequireAuth'
import { SignupPage } from '@/features/auth/SignupPage'
import { BrowsePage } from '@/features/browse/BrowsePage'
import { MoviePage } from '@/features/browse/MoviePage'

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
    <main
      id="main-content"
      tabIndex={-1}
      className="mx-auto flex w-full max-w-5xl flex-col gap-4 px-6 py-10"
    >
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
            <Route element={<AppShell />}>
              <Route path="/" element={<BrowsePage />} />
              <Route path="/movies/:movieId" element={<MoviePage />} />
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
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  )
}

export default App
