import { render, screen, waitFor } from '../testUtils'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AdminPage } from './AdminPage'

const mocks = vi.hoisted(() => ({ getMe: vi.fn() }))

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>()
  return { ...actual, getMe: mocks.getMe }
})

const ADMIN = {
  user_id: 'a1',
  username: 'admin',
  email: 'a@example.com',
  role: 'admin' as const,
  status: 'active' as const,
  created_at: '2026-07-29T00:00:00Z',
}
const MEMBER = { ...ADMIN, user_id: 'u1', username: 'alice', role: 'user' as const }

let replace: ReturnType<typeof vi.fn>
let originalLocation: Location

beforeEach(() => {
  mocks.getMe.mockReset()
  replace = vi.fn()
  originalLocation = window.location
  Object.defineProperty(window, 'location', {
    value: { ...originalLocation, pathname: '/admin', replace },
    writable: true,
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    ),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  Object.defineProperty(window, 'location', { value: originalLocation, writable: true })
})

describe('AdminPage 的访问守卫', () => {
  it('未登录 -> 跳回主页', async () => {
    mocks.getMe.mockResolvedValue(null)
    render(<AdminPage />)
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/'))
  })

  it('登录了但不是 admin -> 跳回主页', async () => {
    mocks.getMe.mockResolvedValue(MEMBER)
    render(<AdminPage />)
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/'))
  })

  it('查身份失败 -> 跳回主页，不停在一个半开的面板上', async () => {
    mocks.getMe.mockRejectedValue(new Error('network down'))
    render(<AdminPage />)
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/'))
  })

  it('核验期间不渲染任何面板内容', async () => {
    // getMe 永不 resolve，模拟核验还在路上
    mocks.getMe.mockReturnValue(new Promise(() => {}))
    render(<AdminPage />)

    expect(screen.queryByRole('navigation', { name: '面板分区' })).toBeNull()
    expect(screen.getByText('正在核验身份…')).toBeInTheDocument()
  })

  it('是 admin -> 正常渲染，不跳转', async () => {
    mocks.getMe.mockResolvedValue(ADMIN)
    render(<AdminPage />)

    await screen.findByRole('heading', { name: '管理面板' })
    expect(replace).not.toHaveBeenCalled()
  })
})

describe('AdminPage 的侧边栏', () => {
  beforeEach(() => {
    mocks.getMe.mockResolvedValue(ADMIN)
  })

  it('四个分区都在', async () => {
    render(<AdminPage />)
    await screen.findByRole('heading', { name: '管理面板' })

    const nav = screen.getByRole('navigation', { name: '面板分区' })
    for (const label of ['用户管理', 'Azure 凭证', '访问白名单', '网站黑名单', '系统状态']) {
      expect(nav).toHaveTextContent(label)
    }
  })

  it('默认停在用户管理，且当前项标了 aria-current', async () => {
    render(<AdminPage />)
    await screen.findByRole('heading', { name: '管理面板' })

    const current = screen.getByRole('button', { name: /用户管理/ })
    expect(current).toHaveAttribute('aria-current', 'page')
  })

  it('点侧边栏切换分区', async () => {
    render(<AdminPage />)
    await screen.findByRole('heading', { name: '管理面板' })

    await userEvent.click(screen.getByRole('button', { name: /访问白名单/ }))

    expect(screen.getByRole('button', { name: /访问白名单/ })).toHaveAttribute(
      'aria-current',
      'page',
    )
    // 白名单面板必须把作用域说清楚：它只管 v1，网页永不受影响。
    // 不写明的话管理员会以为配错就会把整站关掉。
    expect(await screen.findByText(/只作用于 HTTP v1 接口/)).toBeInTheDocument()
  })

  it('有返回上传页的入口', async () => {
    render(<AdminPage />)
    await screen.findByRole('heading', { name: '管理面板' })

    expect(screen.getByRole('link', { name: '返回上传页' })).toHaveAttribute('href', '/')
  })

  it('白名单面板带上「别把自己关在门外」的提示', async () => {
    render(<AdminPage />)
    await screen.findByRole('heading', { name: '管理面板' })

    await userEvent.click(screen.getByRole('button', { name: /访问白名单/ }))

    expect(
      await screen.findByText(/记得把自己解析域名加进来/),
    ).toBeInTheDocument()
  })

  it('黑名单面板说清它比白名单优先、且网页也拦', async () => {
    render(<AdminPage />)
    await screen.findByRole('heading', { name: '管理面板' })

    await userEvent.click(screen.getByRole('button', { name: /网站黑名单/ }))

    expect(await screen.findByText(/网页与 v1 一起拦/)).toBeInTheDocument()
    expect(screen.getByText(/高于白名单/)).toBeInTheDocument()
  })

  it('切到英文时 admin 界面显示英文，不残留中文硬编码', async () => {
    // admin 只做中英两门（其余语言运行时回退英文，见 translate_locales.py
    // 的 CHINESE_ENGLISH_ONLY）。这条守的是「接线漏了某个文案」——漏掉的
    // 那条会以中文形式留在英文界面上，而那正是最不容易被自己发现的：
    // 开发时界面本来就是中文，看不出区别。
    localStorage.setItem('pptx2pdf_locale', 'en')
    mocks.getMe.mockResolvedValue(ADMIN)

    render(<AdminPage />)
    await screen.findByRole('heading', { name: 'Admin panel' })

    const nav = screen.getByRole('navigation', { name: 'Panel sections' })
    for (const label of ['Users', 'Azure credentials', 'Allowlist', 'Blocklist', 'System status']) {
      expect(nav).toHaveTextContent(label)
    }
    expect(nav.textContent ?? '').not.toMatch(/[一-龥]/)

    localStorage.setItem('pptx2pdf_locale', 'zh-CN')
  })
})
