import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AdminPage } from './AdminPage'

const CREDS = {
  tenant_id: 't-1',
  client_id: 'c-1',
  site_id: 's-1',
  drive_path: 'staging',
  secret_configured: true,
}

function mockFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    handler(String(input), init),
  )
  vi.stubGlobal('fetch', spy)
  return spy
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AdminPage', () => {
  it('未登录时显示口令输入框', async () => {
    mockFetch(() => json({ code: 'ADMIN_UNAUTHORIZED' }, 401))
    render(<AdminPage />)
    expect(await screen.findByLabelText('管理口令')).toBeTruthy()
  })

  it('已登录时加载并显示配置，secret 显示为已配置而不回显', async () => {
    mockFetch((url) => (url.includes('graph-credentials') ? json(CREDS) : json({}, 204)))
    render(<AdminPage />)
    await waitFor(() => expect((screen.getByLabelText('租户 ID') as HTMLInputElement).value).toBe('t-1'))
    expect(screen.getByText(/已配置（不回显）/)).toBeTruthy()
  })

  it('口令错误时显示错误而不进入表单', async () => {
    mockFetch((url) => {
      if (url.includes('/login')) return json({ code: 'ADMIN_BAD_PASSWORD', message: '口令错误' }, 401)
      return json({ code: 'ADMIN_UNAUTHORIZED' }, 401)
    })
    render(<AdminPage />)
    const input = await screen.findByLabelText('管理口令')
    await userEvent.type(input, 'wrong')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))
    expect(await screen.findByText(/口令错误/)).toBeTruthy()
  })

  it('保存成功后展示五步全绿', async () => {
    const green = {
      ok: true,
      steps: [
        { step: 'token', ok: true, detail: null },
        { step: 'drive', ok: true, detail: null },
        { step: 'upload', ok: true, detail: null },
        { step: 'convert', ok: true, detail: null },
        { step: 'delete', ok: true, detail: null },
      ],
    }
    mockFetch((url, init) => {
      if (init?.method === 'PUT') return json(green)
      if (url.includes('graph-credentials')) return json(CREDS)
      return json({}, 204)
    })
    render(<AdminPage />)
    await waitFor(() => screen.getByLabelText('租户 ID'))
    await userEvent.click(screen.getByRole('button', { name: '测试并保存' }))
    await waitFor(() => expect(screen.getAllByText('通过').length).toBe(5))
  })

  it('自检失败时逐步展示诊断，未执行的步骤显示为未执行', async () => {
    const failing = {
      code: 'GRAPH_SELFTEST_FAILED',
      message: 'Graph 凭证自检未通过',
      steps: [
        { step: 'token', ok: true, detail: null },
        { step: 'drive', ok: false, detail: '找不到该站点，site_id 可能写错' },
        { step: 'upload', ok: null, detail: null },
        { step: 'convert', ok: null, detail: null },
        { step: 'delete', ok: null, detail: null },
      ],
    }
    mockFetch((url, init) => {
      if (init?.method === 'PUT') return json(failing, 422)
      if (url.includes('graph-credentials')) return json(CREDS)
      return json({}, 204)
    })
    render(<AdminPage />)
    await waitFor(() => screen.getByLabelText('租户 ID'))
    await userEvent.click(screen.getByRole('button', { name: '测试并保存' }))
    expect(await screen.findByText(/site_id 可能写错/)).toBeTruthy()
    expect(screen.getAllByText('未执行').length).toBe(3)
  })

  it('自检进行中禁用保存按钮', async () => {
    let release: (v: Response) => void = () => {}
    const pending = new Promise<Response>((r) => (release = r))
    mockFetch((url, init) => {
      if (init?.method === 'PUT') return pending
      if (url.includes('graph-credentials')) return json(CREDS)
      return json({}, 204)
    })
    render(<AdminPage />)
    await waitFor(() => screen.getByLabelText('租户 ID'))
    const btn = screen.getByRole('button', { name: '测试并保存' })
    await userEvent.click(btn)
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(true))
    release(json({ ok: true, steps: [] }))
  })

  it('服务端未设管理口令时显示未配置提示', async () => {
    mockFetch(() => json({ code: 'ADMIN_NOT_CONFIGURED' }, 503))
    render(<AdminPage />)
    expect(await screen.findByText('管理入口未配置口令')).toBeTruthy()
  })

  it('点击登出后回到未登录态', async () => {
    let loggedOut = false
    mockFetch((url) => {
      if (url.includes('/logout')) {
        loggedOut = true
        return new Response(null, { status: 204 })
      }
      if (url.includes('graph-credentials')) {
        return loggedOut ? json({ code: 'ADMIN_UNAUTHORIZED' }, 401) : json(CREDS)
      }
      return json({}, 204)
    })
    render(<AdminPage />)
    await waitFor(() => screen.getByLabelText('租户 ID'))
    await userEvent.click(screen.getByRole('button', { name: '登出' }))
    expect(await screen.findByLabelText('管理口令')).toBeTruthy()
  })
})
