export interface ChunkRange {
  index: number
  start: number
  end: number
}

export function sliceChunks(size: number, chunkSize: number): ChunkRange[] {
  const ranges: ChunkRange[] = []
  for (let start = 0, index = 0; start < size; start += chunkSize, index += 1) {
    ranges.push({ index, start, end: Math.min(start + chunkSize, size) })
  }
  return ranges
}

const BASE_DELAY_MS = 500
const MAX_DELAY_MS = 10_000

export function backoffDelay(attempt: number): number {
  return Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS)
}

const UNITS = ['B', 'KB', 'MB', 'GB'] as const

export function formatBytes(bytes: number): string {
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  // 舍入到一位小数后可能刚好达到 1024（如 1023.98 -> "1024.0"），需要再提一级单位，
  // 否则会显示违反直觉的 "1024.0 KB" 而不是 "1.0 MB"。
  if (unit < UNITS.length - 1 && Number(value.toFixed(1)) >= 1024) {
    value /= 1024
    unit += 1
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${UNITS[unit]}`
}

export function estimateRemaining(
  bytesSent: number,
  totalBytes: number,
  elapsedMs: number,
): number | null {
  if (bytesSent <= 0 || elapsedMs <= 0) return null
  const rate = bytesSent / elapsedMs
  return Math.round((totalBytes - bytesSent) / rate)
}
