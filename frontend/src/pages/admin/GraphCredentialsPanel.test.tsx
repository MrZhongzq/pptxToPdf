import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { GraphCredentialsPanel } from './GraphCredentialsPanel'

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
  // undici 拒绝给 204 响应带 body（HTTP 规范上 204 不能带 body）——直接
  // new Response(JSON.stringify(body), {status: 204}) 会抛
  // "Response constructor: Invalid response status code 204"。
  return new Response(status === 204 ? null : JSON.stringify(body), {
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

describe('GraphCredentialsPanel（四期的凭证用例，六期随面板拆分搬到这里）', () => {
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
    render(<GraphCredentialsPanel />)
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
    render(<GraphCredentialsPanel />)
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
    render(<GraphCredentialsPanel />)
    await waitFor(() => screen.getByLabelText('租户 ID'))
    const btn = screen.getByRole('button', { name: '测试并保存' })
    await userEvent.click(btn)
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(true))
    release(json({ ok: true, steps: [] }))
  })

})

describe('GraphCredentialsPanel 读配置', () => {
  it('secret 显示为已配置而不回显——解密回显等于把凭证明文发到浏览器', async () => {
    mockFetch((url) => (url.includes('graph-credentials') ? json(CREDS) : json({}, 204)))
    render(<GraphCredentialsPanel />)

    await waitFor(() => expect(screen.getByLabelText('租户 ID')).toHaveValue('t-1'))
    const secret = screen.getByLabelText('客户端密钥') as HTMLInputElement
    expect(secret.value).toBe('')
    expect(secret.placeholder).toMatch(/已配置/)
  })

  it('还没配过时是空表单，不当成错误', async () => {
    mockFetch(() => json({ code: 'GRAPH_NOT_CONFIGURED', message: '未配置' }, 404))
    render(<GraphCredentialsPanel />)

    await waitFor(() => expect(screen.getByLabelText('租户 ID')).toHaveValue(''))
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
