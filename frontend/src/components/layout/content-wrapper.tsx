/**
 * The shared content column (SPEC-7A D10, FR-8 to FR-11).
 *
 * Every screen that uses the shell's main area wraps its content in this
 * component, and the header uses it too, so the page title, the movie blocks and
 * the site name all start on the same vertical line. The column is centered and
 * never wider than 1200 px; horizontal padding is 16 px on phones and 24 px from
 * 768 px.
 */

import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

/** Props of {@link ContentWrapper}. */
export interface ContentWrapperProps {
  /** The content to lay out in the centered column. */
  children: ReactNode
  /** Extra classes for the column (e.g. a flex layout or vertical padding). */
  className?: string
}

/**
 * Render the centered content column.
 *
 * @param props - The content and any extra classes.
 * @returns A `div` that caps its width at 1200 px, centers itself and applies
 *   the shared horizontal padding.
 */
export function ContentWrapper({ children, className }: ContentWrapperProps) {
  return (
    <div className={cn('mx-auto w-full max-w-[1200px] px-4 md:px-6', className)}>
      {children}
    </div>
  )
}
