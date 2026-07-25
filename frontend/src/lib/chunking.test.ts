import { describe, expect, it } from 'vitest'
import {
  backoffDelay,
  estimateRemaining,
  formatBytes,
  sliceChunks,
} from './chunking'

describe('sliceChunks', () => {
  it('splits evenly divisible size', () => {
    expect(sliceChunks(10, 5)).toEqual([
      { index: 0, start: 0, end: 5 },
      { index: 1, start: 5, end: 10 },
    ])
  })

  it('makes the last chunk shorter', () => {
    const chunks = sliceChunks(12, 5)
    expect(chunks).toHaveLength(3)
    expect(chunks[2]).toEqual({ index: 2, start: 10, end: 12 })
  })

  it('handles a file smaller than one chunk', () => {
    expect(sliceChunks(3, 5)).toEqual([{ index: 0, start: 0, end: 3 }])
  })

  it('returns empty for zero size', () => {
    expect(sliceChunks(0, 5)).toEqual([])
  })
})

describe('backoffDelay', () => {
  it('grows exponentially', () => {
    expect(backoffDelay(0)).toBe(500)
    expect(backoffDelay(1)).toBe(1000)
    expect(backoffDelay(2)).toBe(2000)
  })

  it('caps at 10s', () => {
    expect(backoffDelay(20)).toBe(10000)
  })
})

describe('formatBytes', () => {
  it('formats across units', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})

describe('estimateRemaining', () => {
  it('returns null before any progress', () => {
    expect(estimateRemaining(0, 1000, 1000)).toBeNull()
  })

  it('estimates from observed rate', () => {
    // 1000ms 传了 500 字节，剩 500 字节 → 约 1000ms
    expect(estimateRemaining(500, 1000, 1000)).toBe(1000)
  })
})
