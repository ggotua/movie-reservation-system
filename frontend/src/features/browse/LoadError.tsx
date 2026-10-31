/**
 * A data-load failure with its retry action (SPEC-7 FR-14).
 *
 * The message comes from `errorMessages.ts` through the feature's query helper;
 * this component only lays it out, so no code or JSON is ever shown.
 */

import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

/** Props of {@link LoadError}. */
export interface LoadErrorProps {
  /** The friendly message for the failure. */
  message: string
  /** Refetches the failed query. */
  onRetry: () => void
}

/**
 * Render an error message with a "Try again" button.
 *
 * @param props - The message and the retry callback.
 * @returns The alert and the retry button, left aligned in the content column.
 */
export function LoadError({ message, onRetry }: LoadErrorProps) {
  return (
    <div className="flex max-w-xl flex-col items-start gap-3">
      <Alert variant="destructive">
        <AlertDescription>{message}</AlertDescription>
      </Alert>
      <Button
        type="button"
        variant="outline"
        className="h-10 px-4"
        onClick={onRetry}
      >
        Try again
      </Button>
    </div>
  )
}
