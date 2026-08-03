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

    await userEvent.click(screen.getByRole('button', { name: /内置|Built-in/ }))
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([u]) => String(u).includes('include_builtin=true'))).toBe(true),
    )
  })
})
