import { describe, it, expect } from 'vitest'
import { passwordByteLength, MAX_PASSWORD_BYTES } from './password'

describe('password utility', () => {
  it('correctly calculates UTF-8 byte length for various character sets', () => {
    // 72 English ASCII characters = 72 bytes
    const ascii72 = 'a'.repeat(72)
    expect(passwordByteLength(ascii72)).toBe(72)

    // 24 Chinese characters (3 bytes each) = 72 bytes
    const chinese24 = '中'.repeat(24)
    expect(passwordByteLength(chinese24)).toBe(72)

    // 25 Chinese characters (3 bytes each) = 75 bytes
    const chinese25 = '中'.repeat(25)
    expect(passwordByteLength(chinese25)).toBe(75)

    // Emoji: '😀' (U+1F600) is 4 bytes in UTF-8
    const emojiStr = 'hello😀world'
    expect(passwordByteLength(emojiStr)).toBe(5 + 4 + 5) // 14 bytes

    // Empty and null-ish strings
    expect(passwordByteLength('')).toBe(0)
  })

  it('exports MAX_PASSWORD_BYTES as 72', () => {
    expect(MAX_PASSWORD_BYTES).toBe(72)
  })
})
