/**
 * The seat map grid (SPEC-7 FR-3, FR-4, FR-12; design brief Section 4).
 *
 * Draws the screen as a gently curved line above the rows, keeps the row
 * letters visible on both sides while the grid scrolls sideways, and renders
 * every seat as a real `<button>` of at least 40 px. The four states differ in
 * shape as well as colour — a check mark for selected, diagonal hatching for
 * held, an X for taken — so the map still reads in greyscale, in either scheme,
 * and to someone who cannot see the colours. Row letters are decorative for
 * assistive technology because each seat's accessible name already carries its
 * row.
 */

import type { Seat, SeatMap, SeatState } from '@/api/types'
import { cn } from '@/lib/utils'

import { groupSeatsByRow } from './seatSelection'

/** The class list every seat button shares, whatever its state. */
const SEAT_BASE =
  'relative inline-flex size-11 shrink-0 items-center justify-center rounded-t-seat rounded-b-seat-foot border-[1.5px] transition-transform duration-[80ms] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-velvet focus-visible:ring-2 focus-visible:ring-ink motion-reduce:transition-none'

/** The extra classes for each seat state, plus the selected overlay. */
const SEAT_STATE_CLASSES: Readonly<Record<SeatState, string>> = {
  available: 'border-ink bg-paper text-ink hover:bg-aisle/20 active:scale-95',
  held: 'border-ink bg-brass text-ink',
  taken: 'border-aisle bg-aisle text-ink',
}

/** The brass fill of a held seat carries diagonal ink hatching (brief 4). */
const HELD_HATCHING =
  'repeating-linear-gradient(45deg, transparent 0 3px, currentColor 3px 4.5px)'

/** The mark drawn inside a selected seat: a check. */
function CheckMark() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      className="size-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 8.5 6.5 12 13 4.5" />
    </svg>
  )
}

/** The mark drawn inside a taken seat: an X. */
function CrossMark() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      className="size-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
    >
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  )
}

/**
 * Whether a seat is part of the current selection.
 *
 * @param selection - The selected seats.
 * @param seatId - The seat to look for.
 * @returns `true` when the seat is selected.
 */
function isSeatSelected(selection: readonly Seat[], seatId: number): boolean {
  return selection.some((seat) => seat.seat_id === seatId)
}

/**
 * The state word in a seat's accessible name.
 *
 * @param seat - The seat being labelled.
 * @param selected - Whether the seat is currently selected.
 * @returns `'selected'` for a chosen available seat, otherwise the API state
 *   (`'available'`, `'held'` or `'taken'`).
 */
function seatStateWord(seat: Seat, selected: boolean): string {
  return selected ? 'selected' : seat.state
}

/** Props of {@link SeatGrid}. */
export interface SeatGridProps {
  /** The seat map to draw, including the screen dimensions. */
  seatMap: SeatMap
  /** The seats the user has chosen. */
  selection: readonly Seat[]
  /** Called with the activated seat; the page applies the selection rule. */
  onToggle: (seat: Seat) => void
}

/** Props of {@link SeatButton}. */
interface SeatButtonProps {
  /** The seat to draw. */
  seat: Seat
  /** Whether the seat is part of the current selection. */
  selected: boolean
  /** Called when the button is activated. */
  onToggle: (seat: Seat) => void
}

/**
 * Render one seat as a button.
 *
 * @param props - The seat, its selection and the toggle callback.
 * @returns A 44 px button whose name reads like "Row B, seat 4, available".
 *   `held` and `taken` seats are disabled, because pressing them can never
 *   succeed; selection is exposed with `aria-pressed`.
 */
function SeatButton({ seat, selected, onToggle }: SeatButtonProps) {
  const isSelectable = seat.state === 'available'
  const stateWord = seatStateWord(seat, selected)

  return (
    <button
      type="button"
      aria-label={`Row ${seat.row_label}, seat ${seat.seat_number}, ${stateWord}`}
      aria-pressed={isSelectable ? selected : undefined}
      disabled={!isSelectable}
      onClick={() => {
        onToggle(seat)
      }}
      className={cn(
        SEAT_BASE,
        selected
          ? 'border-velvet bg-velvet text-primary-foreground active:scale-95'
          : SEAT_STATE_CLASSES[seat.state],
        !isSelectable && 'cursor-not-allowed',
      )}
      style={
        !selected && seat.state === 'held'
          ? { backgroundImage: HELD_HATCHING }
          : undefined
      }
    >
      {selected && <CheckMark />}
      {!selected && seat.state === 'taken' && <CrossMark />}
    </button>
  )
}

/** Props of {@link RowLabel}. */
interface RowLabelProps {
  /** The row letter, e.g. `"B"`. */
  label: string
  /** Which edge of the scrolling grid the label sticks to. */
  side: 'start' | 'end'
}

/**
 * Render a row letter at an edge of the grid.
 *
 * @param props - The letter and the edge.
 * @returns A sticky label on the `foyer` background, so seats slide under it
 *   while the grid scrolls sideways. Hidden from assistive technology: every
 *   seat's name already says its row.
 */
function RowLabel({ label, side }: RowLabelProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'sticky z-10 flex w-6 shrink-0 items-center justify-center bg-foyer font-display text-3 font-bold text-ink',
        side === 'start' ? 'left-0' : 'right-0',
      )}
    >
      {label}
    </span>
  )
}

/** Props of {@link LegendItem}. */
interface LegendItemProps {
  /** The state the swatch illustrates. */
  state: SeatState
  /** Whether the swatch shows a selected seat instead of the state itself. */
  selected?: boolean
  /** The word shown beside the swatch. */
  children: string
}

/**
 * Render one swatch of the legend with its word.
 *
 * @param props - The state to draw, whether it is the selected look, and the
 *   caption.
 * @returns A list item whose mark matches the seats on the map, so the legend
 *   is usable without colour.
 */
function LegendItem({ state, selected = false, children }: LegendItemProps) {
  return (
    <li className="flex items-center gap-2 text-1">
      <span
        aria-hidden="true"
        className={cn(
          'inline-flex size-6 shrink-0 items-center justify-center rounded-t-[6px] rounded-b-[2px] border-[1.5px]',
          selected
            ? 'border-velvet bg-velvet text-primary-foreground'
            : SEAT_STATE_CLASSES[state],
        )}
        style={
          !selected && state === 'held'
            ? { backgroundImage: HELD_HATCHING }
            : undefined
        }
      >
        {selected && <CheckMark />}
        {!selected && state === 'taken' && <CrossMark />}
      </span>
      {children}
    </li>
  )
}

/**
 * Render the seat map of one showtime.
 *
 * @param props - The map, the current selection and the toggle callback.
 * @returns The screen curve, the rows with sticky letters and the legend.
 *
 * Non-obvious: the aisle gap is inserted after half of a row's seats for
 * layout only. Seat numbers stay continuous, and the gap is decorative, so it
 * is hidden from assistive technology.
 */
export function SeatGrid({ seatMap, selection, onToggle }: SeatGridProps) {
  const rows = groupSeatsByRow(seatMap.seats)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col items-center gap-1">
        <svg
          aria-hidden="true"
          viewBox="0 0 240 12"
          preserveAspectRatio="none"
          className="h-3 w-64 max-w-full text-ink"
        >
          <path
            d="M2 10 Q120 -4 238 10"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          />
        </svg>
        <p className="text-1 text-muted-foreground">Screen</p>
      </div>

      <div className="overflow-x-auto pb-2">
        <div className="inline-flex min-w-full flex-col gap-1.5">
          {rows.map((row) => {
            const aisleAfter = Math.ceil(row.seats.length / 2)
            return (
              <div key={row.rowLabel} className="flex items-center gap-1.5">
                <RowLabel label={row.rowLabel} side="start" />
                <div className="flex items-center gap-1.5">
                  {row.seats.map((seat, index) => (
                    <div
                      key={seat.seat_id}
                      className="flex items-center gap-1.5"
                    >
                      <SeatButton
                        seat={seat}
                        selected={isSeatSelected(selection, seat.seat_id)}
                        onToggle={onToggle}
                      />
                      {index === aisleAfter - 1 &&
                        index < row.seats.length - 1 && (
                          <span aria-hidden="true" className="w-5 shrink-0" />
                        )}
                    </div>
                  ))}
                </div>
                <RowLabel label={row.rowLabel} side="end" />
              </div>
            )
          })}
        </div>
      </div>

      <ul
        aria-label="Seat states"
        className="flex flex-wrap items-center gap-x-4 gap-y-2"
      >
        <LegendItem state="available">Available</LegendItem>
        <LegendItem state="available" selected>
          Selected
        </LegendItem>
        <LegendItem state="held">Held</LegendItem>
        <LegendItem state="taken">Taken</LegendItem>
      </ul>
    </div>
  )
}
