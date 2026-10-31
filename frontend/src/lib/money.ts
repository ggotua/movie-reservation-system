/**
 * Money formatting for display (SPEC-7 FR-10, D5).
 *
 * The API speaks integer cents and the UI never does money arithmetic: only
 * `total_cents` from the server is shown, and it is divided by 100 exactly once
 * here, for display. `Intl.NumberFormat` supplies grouping and the currency
 * marker, so no float arithmetic happens anywhere else.
 */

import { config } from './config'

/**
 * The one money formatter of the app.
 *
 * The locale is pinned to `en-US` because the UI is English only (SPEC-7 D4)
 * and a pinned locale keeps the output identical in a browser, in jsdom and in
 * CI, where the default locale may differ. The currency comes from
 * `VITE_CURRENCY` (read by `lib/config.ts`).
 */
const currencyFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: config.currency,
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

/**
 * Format an integer amount of cents for display.
 *
 * @param cents - The amount in integer cents, e.g. `total_cents` from the API.
 *   Values outside the API's bounds (`0`..`800000000`) are formatted as given;
 *   the function never rounds away fractions of a cent because the API never
 *   sends them.
 * @returns The amount with two decimals and the configured currency, e.g.
 *   `GEL 10.00`. Grouping follows `en-US` (`1,000,000.00`).
 *
 * Non-obvious: the division by 100 is the only float operation in the app's
 * money path and happens after the value has left the API, so values such as
 * 0, 1, 99, 100 and 100000000 cents print as `0.00`, `0.01`, `0.99`, `1.00`
 * and `1,000,000.00` without a floating-point artifact.
 */
export function formatMoney(cents: number): string {
  return currencyFormatter.format(cents / 100)
}
