import { describe, expect, it, vi } from 'vitest'
import { getCapacityConfig } from './api'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('getCapacityConfig', () => {
  it('fetches /api/config/capacity and returns the parsed body', async () => {
    const body = {
      max_file_size: 600 * 1024 * 1024,
      graph_max_shards: 12,
      graph_max_shard_bytes: 40 * 1024 * 1024,
      graph_max_merge_bytes: 240 * 1024 * 1024,
    }
    const fetchSpy = vi.fn(async () => jsonResponse(body))
    vi.stubGlobal('fetch', fetchSpy)

    const result = await getCapacityConfig()

    expect(fetchSpy).toHaveBeenCalledWith('/api/config/capacity')
    expect(result).toEqual(body)

    vi.unstubAllGlobals()
  })

  it('throws ApiError with the code/message from the error body on failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ code: 'INTERNAL_ERROR', message: '挂了' }, 500)),
    )

    await expect(getCapacityConfig()).rejects.toMatchObject({
      code: 'INTERNAL_ERROR',
      message: '挂了',
    })

    vi.unstubAllGlobals()
  })
})
