/**
 * The catch-all screen for an unknown route (SPEC-7 Section 2.3, test 38).
 *
 * It says what happened and where to go next, without apologizing or showing a
 * code (design brief Section 5).
 */

import { Link } from 'react-router-dom'

/**
 * Render the not-found page.
 *
 * @returns A heading, one sentence and a link back to the showtimes.
 */
export function NotFoundPage() {
  return (
    <main
      id="main-content"
      tabIndex={-1}
      className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-4 py-10 sm:px-6"
    >
      <h1 className="text-6">Page not found</h1>
      <p className="text-2">The page you asked for doesn&rsquo;t exist.</p>
      <Link
        to="/"
        className="w-fit text-ink underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink"
      >
        Browse showtimes
      </Link>
    </main>
  )
}
