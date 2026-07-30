import { afterEach, describe, expect, it, vi } from 'vitest'

import { downloadConcurrently, getDownloadSize, shouldDownloadConcurrently } from './api'

const MIB = 1024 * 1024

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('并发下载的启用条件', () => {
  it('小文件不并发——几次握手的开销就吃掉了并行的收益', () => {
    expect(shouldDownloadConcurrently(1 * MIB)).toBe(false)
  })

  it('中等文件并发', () => {
    expect(shouldDownloadConcurrently(20 * MIB)).toBe(true)
  })

  it('超大文件退回原生下载', () => {
    // 并发必须先把整份攒进内存才能拼 Blob，而原生下载是流式的。
    // 几百 MB 的产出宁可慢一点也不能让标签页 OOM。
    expect(shouldDownloadConcurrently(500 * MIB)).toBe(false)
  })

  it('拿不到体积时不并发', () => {
    expect(shouldDownloadConcurrently(null)).toBe(false)
  })
})

describe('getDownloadSize', () => {
  it('从 HEAD 的 Content-Length 取体积', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(null, { status: 200, headers: { 'content-length': '12345' } })),
    )
    expect(await getDownloadSize('t1')).toBe(12345)
  })

  it('HEAD 失败返回 null（调用方据此走原生下载）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 410 })))
    expect(await getDownloadSize('t1')).toBeNull()
  })

  it('没有 Content-Length 返回 null', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 200 })))
    expect(await getDownloadSize('t1')).toBeNull()
  })
})

describe('downloadConcurrently', () => {
  it('按 Range 切成多块并发取，拼回完整字节', async () => {
    const total = 40
    const payload = new Uint8Array(total).map((_, i) => i)
    const seen: string[] = []

    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        const range = ((init?.headers ?? {}) as Record<string, string>).Range
        seen.push(range)
        const m = /bytes=(\d+)-(\d+)/.exec(range)
        if (!m) throw new Error(`Range 头不合法: ${range}`)
        const [, s, e] = m.map(Number)
        return new Response(payload.slice(s, e + 1), { status: 206 })
      }),
    )

    const blob = await downloadConcurrently('t1', total)

    expect(seen).toHaveLength(4)
    // 覆盖必须完整且不重叠——少一个字节产出的就是一份坏 PDF
    const bytes = new Uint8Array(await blob.arrayBuffer())
    expect(bytes).toEqual(payload)
  })

  it('服务端不认 Range（返回 200 而不是 206）时抛错，让调用方回退', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(new Uint8Array(10), { status: 200 })),
    )

    await expect(downloadConcurrently('t1', 10)).rejects.toThrow(/Range/)
  })

  it('汇报累计进度', async () => {
    const total = 40
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        const range = ((init?.headers ?? {}) as Record<string, string>).Range
        const m = /bytes=(\d+)-(\d+)/.exec(range)
        if (!m) throw new Error(`Range 头不合法: ${range}`)
        const [, s, e] = m.map(Number)
        return new Response(new Uint8Array(e - s + 1), { status: 206 })
      }),
    )

    const seen: number[] = []
    await downloadConcurrently('t1', total, (p) => seen.push(p.loaded))

    expect(seen).toHaveLength(4)
    expect(seen[seen.length - 1]).toBe(total)
  })

  it('末块不越界——总长不能被并发数整除时最后一块要短一些', async () => {
    const total = 41 // 41 / 4 = 10.25，切出来的末块必须停在 40
    const ranges: Array<[number, number]> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        const range = ((init?.headers ?? {}) as Record<string, string>).Range
        const m = /bytes=(\d+)-(\d+)/.exec(range)
        if (!m) throw new Error(`Range 头不合法: ${range}`)
        const [, s, e] = m.map(Number)
        ranges.push([s, e])
        return new Response(new Uint8Array(e - s + 1), { status: 206 })
      }),
    )

    await downloadConcurrently('t1', total)

    expect(Math.max(...ranges.map(([, e]) => e))).toBe(total - 1)
    expect(Math.min(...ranges.map(([s]) => s))).toBe(0)
  })
})
