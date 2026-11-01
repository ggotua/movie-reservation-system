/**
 * Unit tests for the seat selection rules (SPEC-7 Section 5.1, tests 8-9).
 *
 * No DOM and no HTTP: the two functions under test decide everything the seat
 * map screen does with a selection, so they are exercised on their own.
 */

import { describe, expect, it } from 'vitest'

import type { Seat, SeatMap, SeatState, SeatType } from '@/api/types'
import {
  MAX_SEATS,
  dropUnavailable,
  groupSeatsByRow,
  toggleSeat,
} from './seatSelection'

/** Fields accepted by the seat fixture helper. */
interface SeatFixture {
  id: number
  row: string
  number: number
  state?: SeatState
  type?: SeatType
}

/**
 * Build a `SeatOut` shaped like the backend's response.
 *
 * @param fixture - The id, row, number and optional state/type.
 * @returns The seat object.
 */
function makeSeat(fixture: SeatFixture): Seat {
  return {
    seat_id: fixture.id,
    row_label: fixture.row,
    seat_number: fixture.number,
    seat_type: fixture.type ?? 'standard',
    state: fixture.state ?? 'available',
  }
}

/**
 * Build a `SeatMapOut` around a list of seats.
 *
 * @param seats - The seats the map contains.
 * @returns The map with a two-row screen.
 */
function makeSeatMap(seats: Seat[]): SeatMap {
  return {
    showtime_id: 11,
    screen: { id: 1, name: 'Screen 1', rows: 2, seats_per_row: 4 },
    seats,
  }
}

describe('seatSelection', () => {
  it('seatSelection_toggle_only_available_seats_max_eight_and_drop_taken', () => {
    const a1 = makeSeat({ id: 1, row: 'A', number: 1 })
    const a2 = makeSeat({ id: 2, row: 'A', number: 2 })
    const a3 = makeSeat({ id: 3, row: 'A', number: 3, state: 'held' })
    const a4 = makeSeat({ id: 4, row: 'A', number: 4, state: 'taken' })

    const added = toggleSeat([], a1, MAX_SEATS)
    expect(added.reason).toBeNull()
    expect(added.selection.map((seat) => seat.seat_id)).toEqual([1])

    const removed = toggleSeat(added.selection, a1, MAX_SEATS)
    expect(removed.reason).toBeNull()
    expect(removed.selection).toEqual([])

    expect(toggleSeat([a1], a3, MAX_SEATS)).toEqual({
      selection: [a1],
      reason: 'not-available',
    })
    expect(toggleSeat([a1], a4, MAX_SEATS)).toEqual({
      selection: [a1],
      reason: 'not-available',
    })

    const eight = Array.from({ length: MAX_SEATS }, (_, index) =>
      makeSeat({ id: 100 + index, row: 'B', number: index + 1 }),
    )
    const filled = eight.reduce(
      (selection, seat) => toggleSeat(selection, seat, MAX_SEATS).selection,
      [] as Seat[],
    )
    expect(filled).toHaveLength(MAX_SEATS)

    const ninth = makeSeat({ id: 109, row: 'B', number: 9 })
    const refused = toggleSeat(filled, ninth, MAX_SEATS)
    expect(refused.reason).toBe('too-many-seats')
    expect(refused.selection).toHaveLength(MAX_SEATS)
    expect(refused.selection).not.toContainEqual(ninth)

    const selection = [a1, a2, makeSeat({ id: 5, row: 'A', number: 5 })]
    const dropped = dropUnavailable(
      selection,
      makeSeatMap([
        makeSeat({ id: 1, row: 'A', number: 1 }),
        makeSeat({ id: 2, row: 'A', number: 2, state: 'taken' }),
      ]),
    )

    expect(dropped.selection.map((seat) => seat.seat_id)).toEqual([1])
    expect(dropped.lostSeats.map((seat) => seat.seat_id)).toEqual([2, 5])
  })

  it('groupSeatsByRow_sorts_rows_and_numbers', () => {
    const seats = [
      makeSeat({ id: 23, row: 'B', number: 3 }),
      makeSeat({ id: 12, row: 'A', number: 2 }),
      makeSeat({ id: 11, row: 'A', number: 1 }),
      makeSeat({ id: 21, row: 'B', number: 1 }),
      makeSeat({ id: 13, row: 'A', number: 3 }),
      makeSeat({ id: 22, row: 'B', number: 2 }),
    ]

    const rows = groupSeatsByRow(seats)

    expect(rows.map((row) => row.rowLabel)).toEqual(['A', 'B'])
    expect(rows.map((row) => row.seats.map((seat) => seat.seat_number))).toEqual(
      [
        [1, 2, 3],
        [1, 2, 3],
      ],
    )
    expect(rows.map((row) => row.seats.map((seat) => seat.seat_id))).toEqual([
      [11, 12, 13],
      [21, 22, 23],
    ])
    expect(groupSeatsByRow([])).toEqual([])
  })
})
