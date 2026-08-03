import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '../../i18n'
import { FontsPanel } from './FontsPanel'

const FONT = {
  file_id: 'bWFuYWdlZC9tc3loLnR0Yw',
  filename: 'msyh.ttc',
  source: 'managed',
  families: ['微软雅黑', 'Microsoft YaHei UI'],
  faces: [{ family: '微软雅黑', style: 'Regular' }],
  version: '6.30',
  charset_count: 28762,
  size_bytes: 19_000_000,
  modified_at: '2026-08-03T00:00:00Z',
  deletable: true,
}

const MOUNTED = { ...FONT, file_id: 'bW91bnRlZC9hLnR0Zg', filename: 'a.ttf', source: 'mounted', deletable: false }

const BUILTIN_FONT = {
  ...FONT,
  file_id: 'YnVpbHRpbi9iLnR0Zg',
  filename: 'b.ttf',
  source: 'builtin',
  deletable: false,
}

function renderPanel() {
  return render(
    <I18nProvider>
      <FontsPanel />
    </I18nProvider>,
  )
}

describe('FontsPanel', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('lists managed fonts with their families and coverage', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ managed: [FONT], mounted: [], builtin: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))

    renderPanel()

    expect(await screen.findByText('msyh.ttc')).toBeInTheDocument()
    expect(screen.getByText(/微软雅黑/)).toBeInTheDocument()
    expect(screen.getByText(/28,762/)).toBeInTheDocument()
  })

  it('does not offer delete on fonts it cannot delete', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ managed: [FONT], mounted: [MOUNTED], builtin: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))

    renderPanel()

    await screen.findByText('a.ttf')
    // 只有 managed 那一个有删除按钮
    expect(screen.getAllByRole('button', { name: /删除|Delete/ })).toHaveLength(1)
  })

  it('asks for confirmation before deleting', async () => {
    const fetchSpy = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') return new Response(null, { status: 204 })
      return new Response(JSON.stringify({ managed: [FONT], mounted: [], builtin: [] }),
        { status: 200, headers: { 'Content-Type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fetchSpy)
    vi.stubGlobal('confirm', vi.fn(() => false))

    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: /删除|Delete/ }))

    // 用户取消了，不应该发出 DELETE
    expect(fetchSpy.mock.calls.every(([, init]) => init?.method !== 'DELETE')).toBe(true)
  })

  it('loads builtin fonts only when the group is expanded', async () => {
    const fetchSpy = vi.fn(async () => new Response(
      JSON.stringify({ managed: [], mounted: [], builtin: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchSpy)

    renderPanel()
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    expect(String(fetchSpy.mock.calls[0][0])).not.toContain('include_builtin=true')

    await userEvent.click(screen.getByRole('button', { name: /内置|built-in/i }))
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([u]) => String(u).includes('include_builtin=true'))).toBe(true),
    )
  })

  it('keeps the expanded builtin list even if the stale initial request resolves later', async () => {
    // 挂载时发出的初始请求（不含 builtin）故意卡住不 resolve，模拟它比
    // 「展开内置」那次请求慢的乱序场景。展开按钮在初始请求落地前就能点，
    // 所以两个请求会并发在途。
    let resolveInitial!: (r: Response) => void
    const initial = new Promise<Response>((resolve) => {
      resolveInitial = resolve
    })

    const fetchSpy = vi.fn(async (url: string) => {
      if (String(url).includes('include_builtin=true')) {
        return new Response(
          JSON.stringify({ managed: [], mounted: [], builtin: [BUILTIN_FONT] }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      return initial
    })
    vi.stubGlobal('fetch', fetchSpy)

    renderPanel()

    // 初始请求还没落地，就点了展开——这是竞态能发生的前提。
    await userEvent.click(screen.getByRole('button', { name: /内置|built-in/i }))
    await screen.findByText('b.ttf')

    // 现在才放行那个过期的初始响应。如果没有世代号防护，它落地时会用
    // 「不含 builtin」的旧数据覆盖已经展开的列表，b.ttf 就会从界面消失。
    resolveInitial(
      new Response(JSON.stringify({ managed: [], mounted: [], builtin: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2))
    // 给过期响应的 .then 一个机会跑完（应该是空操作）。
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.getByText('b.ttf')).toBeInTheDocument()
  })
})
