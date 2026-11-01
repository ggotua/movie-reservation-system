/**
 * Seat selection and grouping rules (SPEC-7 Section 2.5, Section 10 DRY
 * check).
 *
 * This is the only place that decides what a click on a seat does and how the
 * flat seat list becomes rows. Every function is pure: it takes the current
 * selection and returns a new one instead of mutating what it was given, so a
 * React component can hold the selection in state without surprises.
 */

import type { Seat, SeatMap } from '@/api/types'

/**
 * The most seats one hold may contain.
 *
 * Mirrors the backend's `MAX_SEATS_PER_HOLD` default (SPEC-7 D6). The server is
 * still the authority: if it answers `TOO_MANY_SEATS`, the UI shows that
 * message rather than guessing.
 */
export const MAX_SEATS = 8

/** Why {@link toggleSeat} left the selection unchanged. */
export type ToggleSeatRefusal = 'not-available' | 'too-many-seats'

/** The outcome of a toggle: always the selection to use next. */
export interface ToggleSeatResult {
  /** The selection after the click; unchanged when `reason` is set. */
  selection: Seat[]
  /**
   * `null` when the click was applied, `'not-available'` when the seat is
   * `held`/`taken`, `'too-many-seats'` when adding it would exceed the limit.
   */
  reason: ToggleSeatRefusal | null
}

/** The outcome of dropping seats that are no longer available. */
export interface DropUnavailableResult {
  /** The selected seats that are still `available`, in their original order. */
  selection: Seat[]
  /** The seats that were removed, in their original order. */
  lostSeats: Seat[]
}

/** One row of the grid: its letter and its seats ordered by number. */
export interface SeatRow {
  /** The row letter, e.g. `"B"`. */
  rowLabel: string
  /** The row's seats, ordered by `seat_number` ascending. */
  seats: Seat[]
}

/**
 * Select or deselect one seat.
 *
 * @param selection - The current selection; not modified.
 * @param seat - The seat that was activated.
 * @param max - The most seats the selection may hold, usually
 *   {@link MAX_SEATS}.
 * @returns The new selection. When the seat is already selected it is removed
 *   and `reason` is `null`, so deselecting still works at the limit. Otherwise
 *   only an `available` seat is added; a `held`/`taken` seat is refused with
 *   `'not-available'` and a full selection with `'too-many-seats'`.
 */
export function toggleSeat(
  selection: readonly Seat[],
  seat: Seat,
  max: number,
): ToggleSeatResult {
  const isSelected = selection.some((chosen) => chosen.seat_id === seat.seat_id)

  if (isSelected) {
    return {
      selection: selection.filter((chosen) => chosen.seat_id !== seat.seat_id),
      reason: null,
    }
  }

  if (seat.state !== 'available') {
    return { selection: [...selection], reason: 'not-available' }
  }

  if (selection.length >= max) {
    return { selection: [...selection], reason: 'too-many-seats' }
  }

  return { selection: [...selection, seat], reason: null }
}

/**
 * Remove from the selection every seat that is no longer `available`.
 *
 * @param selection - The current selection; not modified.
 * @param seatMap - The freshest seat map, e.g. from a poll or a refresh after
 *   `SEAT_UNAVAILABLE`.
 * @returns The surviving selection and the seats that were lost. A selected
 *   seat missing from the map counts as lost: the screen no longer exposes it.
 *
 * Non-obvious: a surviving seat is replaced by the map's own object, so the
 * selection always carries the latest `state` and cannot go stale.
 */
export function dropUnavailable(
  selection: readonly Seat[],
  seatMap: SeatMap,
): DropUnavailableResult {
  const currentSeats = new Map(seatMap.seats.map((seat) => [seat.seat_id, seat]))
  const kept: Seat[] = []
  const lost: Seat[] = []

  for (const chosen of selection) {
    const current = currentSeats.get(chosen.seat_id)
    if (current !== undefined && current.state === 'available') {
      kept.push(current)
    } else {
      lost.push(chosen)
    }
  }

  return { selection: kept, lostSeats: lost }
}

/**
 * Group a flat seat list into rows for the grid.
 *
 * @param seats - The seats of a `SeatMapOut`, in any order.
 * @returns One entry per row, rows sorted by label and seats sorted by number,
 *   so the drawing does not depend on the API's ordering.
 */
export function groupSeatsByRow(seats: readonly Seat[]): SeatRow[] {
  const rows = new Map<string, Seat[]>()
  for (const seat of seats) {
    const row = rows.get(seat.row_label)
    if (row === undefined) {
      rows.set(seat.row_label, [seat])
    } else {
      row.push(seat)
    }
  }

  return [...rows.entries()]
    .map(([rowLabel, rowSeats]) => ({
      rowLabel,
      seats: [...rowSeats].sort((a, b) => a.seat_number - b.seat_number),
    }))
    .sort((a, b) => a.rowLabel.localeCompare(b.rowLabel))
}
