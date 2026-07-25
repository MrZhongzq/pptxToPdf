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

  it('preserves chunk-size invariants at real-world scale (evenly divisible)', () => {
    const size = 524288000 // 500 MiB
    const chunkSize = 5242880 // 5 MiB
    const chunks = sliceChunks(size, chunkSize)

    // 循环条件写成 `start <= size` 而非 `start < size` 会在整除场景多产出
    // 一个零长度尾块；下面的 totalLength/end 断言对此不敏感（多出的空块
    // 贡献 0 长度，且 forEach 里 `i < chunks.length - 1` 会把它排除在
    // 相邻性检查之外），必须显式断言块数才能拦住这个 mutation。
    expect(chunks).toHaveLength(Math.ceil(size / chunkSize))

    const totalLength = chunks.reduce((sum, c) => sum + (c.end - c.start), 0)
    expect(totalLength).toBe(size)

    expect(chunks[0].start).toBe(0)
    expect(chunks[chunks.length - 1].end).toBe(size)

    chunks.forEach((chunk, i) => {
      expect(chunk.index).toBe(i)
      if (i < chunks.length - 1) {
        expect(chunk.end).toBe(chunks[i + 1].start)
        expect(chunk.end - chunk.start).toBe(chunkSize)
      }
    })
  })

  it('preserves chunk-size invariants at real-world scale (non-divisible)', () => {
    const size = 524288001 // 500 MiB + 1 byte
    const chunkSize = 5242880 // 5 MiB
    const chunks = sliceChunks(size, chunkSize)

    expect(chunks).toHaveLength(Math.ceil(size / chunkSize))

    const totalLength = chunks.reduce((sum, c) => sum + (c.end - c.start), 0)
    expect(totalLength).toBe(size)

    expect(chunks[0].start).toBe(0)
    expect(chunks[chunks.length - 1].end).toBe(size)

    chunks.forEach((chunk, i) => {
      expect(chunk.index).toBe(i)
      if (i < chunks.length - 1) {
        expect(chunk.end).toBe(chunks[i + 1].start)
        expect(chunk.end - chunk.start).toBe(chunkSize)
      }
    })
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

  it('rounds up to the next unit at the unit boundary instead of showing 1024.0', () => {
    // 舍入到 KB 后恰好达到 1024.0 KB，应提升到 1.0 MB
    expect(formatBytes(1048574.976)).toBe('1.0 MB')
    // 舍入到 MB 后恰好达到 1024.0 MB，应提升到 1.0 GB
    expect(formatBytes(1073741823)).toBe('1.0 GB')
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
