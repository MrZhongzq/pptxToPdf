import { beforeEach, describe, expect, it, vi } from 'vitest'
import { uploadFile } from './uploadClient'

const CHUNK = 4

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeFile(size: number): File {
  return new File([new Uint8Array(size)], 'deck.pptx')
}

describe('uploadFile', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('uploads every chunk and returns the task id', async () => {
    const sent: number[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url === '/api/uploads')
          return jsonResponse({
            upload_id: 'u1',
            chunk_size: CHUNK,
            total_chunks: 3,
            expires_at: '2030-01-01T00:00:00Z',
          })
        if (url.includes('/chunks/')) {
          sent.push(Number(url.split('/').pop()))
          return jsonResponse({ index: 0, received_count: sent.length })
        }
        if (url.endsWith('/complete')) return jsonResponse({ task_id: 't1' })
        if (init?.method === undefined)
          return jsonResponse({
            received_indices: [],
            bytes_received: 0,
            total_chunks: 3,
            chunk_size: CHUNK,
            status: 'active',
          })
        return jsonResponse({}, 500)
      }),
    )

    const result = await uploadFile(makeFile(10))

    expect(result.taskId).toBe('t1')
    expect(sent.sort()).toEqual([0, 1, 2])
  })

  it('skips chunks the server already has', async () => {
    const sent: number[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/uploads')
          return jsonResponse({
            upload_id: 'u1',
            chunk_size: CHUNK,
            total_chunks: 3,
            expires_at: '2030-01-01T00:00:00Z',
          })
        if (url === '/api/uploads/u1')
          return jsonResponse({
            received_indices: [0, 1],
            bytes_received: 8,
            total_chunks: 3,
            chunk_size: CHUNK,
            status: 'active',
          })
        if (url.includes('/chunks/')) {
          sent.push(Number(url.split('/').pop()))
          return jsonResponse({ index: 0, received_count: 3 })
        }
        return jsonResponse({ task_id: 't1' })
      }),
    )

    await uploadFile(makeFile(10), { resumeUploadId: 'u1' })

    expect(sent).toEqual([2])
  })

  it('retries a failing chunk then succeeds', async () => {
    let chunkAttempts = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/uploads')
          return jsonResponse({
            upload_id: 'u1',
            chunk_size: CHUNK,
            total_chunks: 1,
            expires_at: '2030-01-01T00:00:00Z',
          })
        if (url.includes('/chunks/')) {
          chunkAttempts += 1
          if (chunkAttempts < 3) return jsonResponse({ code: 'X' }, 500)
          return jsonResponse({ index: 0, received_count: 1 })
        }
        return jsonResponse({ task_id: 't1' })
      }),
    )

    const result = await uploadFile(makeFile(3), { retryBaseMs: 0 })

    expect(chunkAttempts).toBe(3)
    expect(result.taskId).toBe('t1')
  })

  it('reports progress monotonically', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/uploads')
          return jsonResponse({
            upload_id: 'u1',
            chunk_size: CHUNK,
            total_chunks: 3,
            expires_at: '2030-01-01T00:00:00Z',
          })
        if (url.includes('/chunks/'))
          return jsonResponse({ index: 0, received_count: 1 })
        return jsonResponse({ task_id: 't1' })
      }),
    )

    const seen: number[] = []
    await uploadFile(makeFile(10), {
      onProgress: (p) => seen.push(p.bytesSent),
    })

    expect(seen.at(-1)).toBe(10)
    expect([...seen].sort((a, b) => a - b)).toEqual(seen)
  })
})
