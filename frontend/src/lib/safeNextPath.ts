/**
 * Validation of the `next` redirect path (SPEC-7 FR-11, Section 2.3).
 *
 * After a login the app returns the user to `?next=<path>`. Only a same-origin
 * relative path is accepted, so a crafted link cannot redirect the user to
 * another site or run a `javascript:` URL.
 */

/** Returned whenever the supplied value is not an acceptable relative path. */
const FALLBACK_PATH = '/'

/**
 * Whether a string holds an ASCII control character (newline, NUL, DEL).
 *
 * A character-code loop rather than a regex: ESLint's `no-control-regex` rule
 * forbids a control-character range in a pattern, and the loop is just as
 * clear.
 *
 * @param value - The string to check.
 * @returns `true` when any character is below `0x20` or is `0x7f`.
 */
function hasControlCharacter(value: string): boolean {
  return [...value].some((character) => {
    const code = character.charCodeAt(0)
    return code < 0x20 || code === 0x7f
  })
}

/**
 * Accept a same-origin relative path, or fall back to `/`.
 *
 * @param value - The raw `next` query parameter, possibly missing.
 * @returns `value` unchanged when it starts with exactly one `/`, carries no
 *   scheme and no backslash, and holds no control characters; otherwise `/`.
 *
 * Non-obvious edge cases:
 * - `//evil.example` and `/\evil.example` are protocol-relative or browser-
 *   normalized to one, so both are rejected;
 * - `https://evil.example` and `javascript:alert(1)` do not start with `/`;
 * - an empty, `null` or `undefined` value yields `/`;
 * - a query string or fragment on an accepted path is preserved
 *   (`/checkout/5?from=login`).
 */
export function safeNextPath(value: string | null | undefined): string {
  if (typeof value !== 'string' || value.length === 0) {
    return FALLBACK_PATH
  }
  if (!value.startsWith('/') || value.startsWith('//')) {
    return FALLBACK_PATH
  }
  if (value.includes('\\') || hasControlCharacter(value)) {
    return FALLBACK_PATH
  }
  return value
}
