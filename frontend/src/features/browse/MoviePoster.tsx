/**
 * A movie poster with a neutral fallback (SPEC-7 Section 3, edge cases).
 *
 * `poster_url` is only ever used as an `<img src>` (FR-11). A missing URL and a
 * broken image both render the same quiet block, so a dead link cannot leave a
 * broken-image icon in the layout.
 */

import { useState } from 'react'

import { cn } from '@/lib/utils'

/** Props of {@link MoviePoster}. */
export interface MoviePosterProps {
  /** `poster_url` from the API, or `null` when the movie has no poster. */
  src: string | null
  /** The movie title, used as the image's alternative text. */
  title: string
  /** Sizing classes from the caller, e.g. `w-20 sm:w-28`. */
  className?: string
}

/**
 * Render a movie poster, falling back to a plain block.
 *
 * @param props - The poster URL, the movie title and sizing classes.
 * @returns An `<img>` with lazy loading, or an empty `aria-hidden` block when
 *   the poster is missing or the browser failed to load it.
 *
 * Non-obvious: the failed URL is remembered instead of a boolean, so a new
 * poster for the same component position is tried again without an effect.
 */
export function MoviePoster({ src, title, className }: MoviePosterProps) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const sizeClasses = cn('shrink-0 rounded-poster border border-aisle', className)

  if (src === null || src === '' || failedSrc === src) {
    return <div aria-hidden="true" className={cn(sizeClasses, 'aspect-[2/3] bg-paper')} />
  }

  return (
    <img
      src={src}
      alt={`Poster for ${title}`}
      loading="lazy"
      onError={() => setFailedSrc(src)}
      className={cn(sizeClasses, 'aspect-[2/3] bg-paper object-cover')}
    />
  )
}
