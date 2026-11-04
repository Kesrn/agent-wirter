export const MAX_PASSWORD_BYTES = 72

/**
 * Calculates UTF-8 byte length of a string using TextEncoder.
 */
export function passwordByteLength(s: string): number {
  if (!s) return 0
  return new TextEncoder().encode(s).length
}
