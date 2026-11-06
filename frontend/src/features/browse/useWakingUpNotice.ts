/**
 * The cold-start notice for the browse page (SPEC-7B D8, FR-4).
 *
 * The backend runs on a free host that sleeps when it has no traffic and needs
 * about a minute to wake. A visitor should not be left guessing why the first
 * load is slow, so once the browse request has been pending for longer than
 * {@link WAKING_UP_NOTICE_DELAY_MS} the page says so. The notice is display
 * only: the request itself is never interrupted, and the line disappears as
 * soon as the request succeeds or fails.
 */

import { useEffect, useState } from 'react'

/** How long a request may stay pending before the notice appears. */
export const WAKING_UP_NOTICE_DELAY_MS = 4000

/** The sentence the browse page shows while the server may be waking up. */
export const WAKING_UP_NOTICE_TEXT =
  'Waking up the server. This can take up to a minute.'

/**
 * Report whether a request has been pending for longer than the notice delay.
 *
 * @param isPending - Whether the request is still running.
 * @param delayMs - How long to wait before reporting the delay; defaults to
 *   {@link WAKING_UP_NOTICE_DELAY_MS}.
 * @returns `true` only while `isPending` is `true` and `delayMs` has passed
 *   since the request started pending. A finished request reports `false` at
 *   once, and the next pending request starts the wait from zero again.
 */
export function useWakingUpNotice(
  isPending: boolean,
  delayMs: number = WAKING_UP_NOTICE_DELAY_MS,
): boolean {
  const [isSlow, setIsSlow] = useState(false)

  useEffect(() => {
    if (!isPending) {
      return
    }
    const timer = window.setTimeout(() => {
      setIsSlow(true)
    }, delayMs)
    return () => {
      window.clearTimeout(timer)
      setIsSlow(false)
    }
  }, [isPending, delayMs])

  return isPending && isSlow
}
