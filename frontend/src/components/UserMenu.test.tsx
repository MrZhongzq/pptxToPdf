import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { UserMenu } from './UserMenu'

const mocks = vi.hoisted(() => ({
  getMe: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>()
  return { ...actual, getMe: mocks.getMe, login: mocks.login, logout: mocks.logout }
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

describe('UserMenu', () => {
  beforeEach(() => {
    mocks.getMe.mockReset().mockResolvedValue(null)
    mocks.login.mockReset()
    mocks.logout.mockReset().mockResolvedValue(undefined)
  })

  it('未登录时显示登录按钮，不显示 admin 面板入口', async () => {
    render(<UserMenu />)
    await waitFor(() => expect(mocks.getMe).toHaveBeenCalled())

    expect(screen.getByRole('button', { name: '登录 / 注册' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'admin 面板' })).not.toBeInTheDocument()
  })

  it('普通用户登录后显示用户名，但没有 admin 面板入口', async () => {
    mocks.getMe.mockResolvedValue(MEMBER)
    render(<UserMenu />)

    await screen.findByText('alice')
    expect(screen.queryByRole('link', { name: 'admin 面板' })).not.toBeInTheDocument()
  })

  it('admin 登录后多出 admin 面板按钮，指向 /admin', async () => {
    mocks.getMe.mockResolvedValue(ADMIN)
    render(<UserMenu />)

    const link = await screen.findByRole('link', { name: 'admin 面板' })
    expect(link).toHaveAttribute('href', '/admin')
  })

  it('点注册只弹一句说明，不发任何请求', async () => {
    render(<UserMenu />)
    await waitFor(() => expect(mocks.getMe).toHaveBeenCalled())

    await userEvent.click(screen.getByRole('button', { name: '登录 / 注册' }))
    await userEvent.click(screen.getByRole('button', { name: '注册' }))

    expect(
      screen.getByText('当前网站未开放注册，如需账号请直接联系网站管理员。'),
    ).toBeInTheDocument()
    expect(mocks.login).not.toHaveBeenCalled()
  })

  it('登录成功后把用户回调给父组件', async () => {
    mocks.login.mockResolvedValue(MEMBER)
    const onUserChange = vi.fn()
    render(<UserMenu onUserChange={onUserChange} />)
    await waitFor(() => expect(mocks.getMe).toHaveBeenCalled())

    await userEvent.click(screen.getByRole('button', { name: '登录 / 注册' }))
    await userEvent.type(screen.getByPlaceholderText('用户名'), 'alice')
    await userEvent.type(screen.getByPlaceholderText('密码'), 'hunter2!')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    await waitFor(() => expect(onUserChange).toHaveBeenCalledWith(MEMBER))
  })

  it('登录失败时把服务端的消息显示出来，不吞掉', async () => {
    const { ApiError } = await import('../lib/api')
    mocks.login.mockRejectedValue(new ApiError('BAD_CREDENTIALS', '用户名或密码错误', 401))
    render(<UserMenu />)
    await waitFor(() => expect(mocks.getMe).toHaveBeenCalled())

    await userEvent.click(screen.getByRole('button', { name: '登录 / 注册' }))
    await userEvent.type(screen.getByPlaceholderText('用户名'), 'alice')
    await userEvent.type(screen.getByPlaceholderText('密码'), 'wrong')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('用户名或密码错误')
  })

  it('退出后回到未登录状态', async () => {
    mocks.getMe.mockResolvedValue(MEMBER)
    const onUserChange = vi.fn()
    render(<UserMenu onUserChange={onUserChange} />)
    await screen.findByText('alice')

    await userEvent.click(screen.getByRole('button', { name: '退出' }))

    await waitFor(() => expect(onUserChange).toHaveBeenLastCalledWith(null))
    expect(screen.getByRole('button', { name: '登录 / 注册' })).toBeInTheDocument()
  })

  it('查当前用户失败不该让组件崩掉——那只是没登录', async () => {
    mocks.getMe.mockRejectedValue(new Error('network down'))
    render(<UserMenu />)

    expect(await screen.findByRole('button', { name: '登录 / 注册' })).toBeInTheDocument()
  })
})
