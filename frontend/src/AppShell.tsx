/**
 * The app shell: a skip link, the site header and the routed page below it
 * (SPEC-7 prompt 5, FR-12).
 *
 * The header uses the display face for the site name and plain links for
 * navigation and the session actions. It wraps on a phone width instead of
 * hiding behind a menu, and the skip link is the first focusable element so a
 * keyboard user can jump past the header on every page.
 */

import { Link, NavLink, Outlet } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/auth/AuthContext'
import { cn } from '@/lib/utils'

/** The site name shown in the header; also the document title. */
const SITE_NAME = 'Movie Reservation System'

/**
 * Style a header navigation link, marking the active one.
 *
 * @param props - React Router's render props for the link.
 * @returns Classes for the link; the active link is underlined as well as
 *   carrying `aria-current`, so it is not distinguished by color alone.
 */
function navLinkClass({ isActive }: { isActive: boolean }): string {
  return cn(
    'flex h-10 items-center text-1 font-medium focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink',
    isActive
      ? 'text-ink underline underline-offset-4'
      : 'text-muted-foreground hover:text-ink',
  )
}

/**
 * Render the shell around the current route.
 *
 * @returns The skip link, the header and the page rendered by the router.
 */
export function AppShell() {
  const { token, user, logout } = useAuth()
  const isLoggedIn = token !== null

  return (
    <div className="flex min-h-svh flex-col">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-control focus:border focus:border-ink focus:bg-paper focus:px-4 focus:py-2 focus:text-ink"
      >
        Skip to content
      </a>

      <header className="border-b border-aisle bg-paper">
        <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center gap-x-6 gap-y-1 px-4 py-2 sm:px-6">
          <Link
            to="/"
            className="font-display text-4 font-extrabold focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink"
          >
            {SITE_NAME}
          </Link>

          <nav aria-label="Main" className="flex items-center gap-4">
            <NavLink to="/" end className={navLinkClass}>
              Browse
            </NavLink>
            {isLoggedIn && (
              <NavLink to="/reservations" className={navLinkClass}>
                My reservations
              </NavLink>
            )}
          </nav>

          <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1">
            {isLoggedIn ? (
              <>
                {user !== null && (
                  <span className="max-w-48 truncate text-1 text-muted-foreground">
                    {user.email}
                  </span>
                )}
                <Button
                  type="button"
                  variant="ghost"
                  className="h-10 px-3"
                  onClick={logout}
                >
                  Log out
                </Button>
              </>
            ) : (
              <>
                <NavLink to="/login" className={navLinkClass}>
                  Log in
                </NavLink>
                <NavLink to="/signup" className={navLinkClass}>
                  Sign up
                </NavLink>
              </>
            )}
          </div>
        </div>
      </header>

      <Outlet />
    </div>
  )
}
