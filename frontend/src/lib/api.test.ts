import { afterEach, describe, expect, it, vi } from 'vitest'
import { getCapacityConfig, putChunk } from './api'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('getCapacityConfig', () => {
  // 审查 Minor #8：还原放在断言之后会有一个坑——断言一旦抛错，
  // vi.unstubAllGlobals() 就不会执行，全局 fetch 桩会漏到下一个测试里。
  // 挪进 afterEach，不管本用例成功还是失败都会执行。
  afterEach(() => {
    vi.unstubAllGlobals()
  })

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
  })
})

describe('putChunk', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  /**
   * iOS WebKit 的坑：上传一个从 File.slice() 切出来的 Blob 时，响应**体**
   * 的读取永远不会完成——fetch 的 promise 在响应头到达时正常 resolve，
   * 但紧接着的 resp.json() 就再也回不来了。真机实测（iPad OS 26 /
   * WebKit 605.1.15）：换 XMLHttpRequest 一样死，因为 onload 也要等
   * 完整响应，所以这不是 fetch 的问题，是网络层的。
   *
   * 症状是整个上传永久卡住：Promise.all 里的那个 worker 永不返回，
   * complete 请求从此发不出去，服务端那边分片其实早就收全了。
   *
   * 所以成功路径一个字节的 body 都不能读。putChunk 返回 void，本来也
   * 不需要响应内容。
   */
  it('resolves without reading the body when the server returns ok', async () => {
    let bodyRead = false
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: () => {
          bodyRead = true
          return new Promise(() => {}) // 永不 resolve，正如真机上那样
        },
        text: () => {
          bodyRead = true
          return new Promise(() => {})
        },
      })),
    )

    await expect(putChunk('u1', 0, new Blob(['x']))).resolves.toBeUndefined()
    expect(bodyRead).toBe(false)
  })

  /** 失败路径仍要给出可读的错误，但同样不能被永远挂住的 body 拖死。 */
  it('still surfaces an ApiError when the server rejects the chunk', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ code: 'UPLOAD_SESSION_NOT_FOUND', message: '没了' }, 404)),
    )

    await expect(putChunk('u1', 0, new Blob(['x']))).rejects.toMatchObject({
      code: 'UPLOAD_SESSION_NOT_FOUND',
      status: 404,
    })
  })

  it('falls back to the status code when the error body also hangs', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 503,
        statusText: 'Service Unavailable',
        json: () => new Promise(() => {}),
      })),
    )

    await expect(putChunk('u1', 0, new Blob(['x']))).rejects.toMatchObject({ status: 503 })
  })
})
