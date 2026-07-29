import { useEffect, useRef, useState, type FormEvent } from 'react'

import { ApiError, getMe, login, logout, type UserDto } from '../lib/api'

/**
 * 右上角的用户入口。
 *
 * 未登录显示「登录」；登录后显示用户名，admin 额外多一个「admin 面板」
 * 按钮。注册按钮存在但点了只弹一句说明——站点当前不开放注册，后端
 * 刻意没有注册端点（留一个关着的端点只是凭空多一个攻击面）。
 */
export function UserMenu({ onUserChange }: { onUserChange?: (u: UserDto | null) => void }) {
  const [user, setUser] = useState<UserDto | null>(null)
  const [open, setOpen] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const dialogRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let alive = true
    getMe()
      .then((u) => {
        if (!alive) return
        setUser(u)
        onUserChange?.(u)
      })
      .catch(() => {
        /* 拿不到当前用户就当未登录——这个查询失败不该阻塞整个页面 */
      })
    return () => {
      alive = false
    }
    // onUserChange 由父组件每次渲染重建，放进依赖会让这个 effect 每次都重跑，
    // 于是每次渲染都打一次 /api/auth/me。这里只需要挂载时查一次。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 点弹窗外面关掉。不用 <dialog>：它的原生 backdrop 在 Safari 上
  // 表现不一致，而这个弹窗结构很简单，自己管更省心。
  useEffect(() => {
    if (!open) return
    function onDocClick(e: MouseEvent) {
      if (dialogRef.current && !dialogRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open])

  async function handleLogin(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const u = await login(username.trim(), password)
      setUser(u)
      onUserChange?.(u)
      setOpen(false)
      setUsername('')
      setPassword('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '登录失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }

  async function handleLogout() {
    await logout()
    setUser(null)
    onUserChange?.(null)
  }

  return (
    <div style={{ position: 'relative', display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
      {user?.role === 'admin' && (
        <a className="btn btn-ghost" href="/admin">
          admin 面板
        </a>
      )}

      {user ? (
        <>
          <span style={{ fontSize: 13, color: 'var(--c-text-muted)' }}>{user.username}</span>
          <button type="button" className="btn btn-ghost" onClick={handleLogout}>
            退出
          </button>
        </>
      ) : (
        <button
          type="button"
          className="btn btn-ghost"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          {/* 与弹窗里的提交按钮区分开：两个可访问名相同会让屏幕阅读器
              用户分不清「打开面板」和「提交表单」。文案也更贴合需求
              原文的「登录/注册」。 */}
          登录 / 注册
        </button>
      )}

      {open && !user && (
        <div
          ref={dialogRef}
          className="card"
          role="dialog"
          aria-label="登录"
          style={{
            position: 'absolute',
            top: 'calc(100% + var(--space-2))',
            right: 0,
            width: 280,
            padding: 'var(--space-4)',
            zIndex: 20,
          }}
        >
          <form onSubmit={handleLogin} style={{ display: 'grid', gap: 'var(--space-3)' }}>
            <span className="section-title">登录</span>
            <input
              className="input"
              placeholder="用户名"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              disabled={busy}
            />
            <input
              className="input"
              type="password"
              placeholder="密码"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              disabled={busy}
            />
            {error && (
              <p role="alert" className="alert alert-danger" style={{ margin: 0 }}>
                {error}
              </p>
            )}
            {notice && (
              <p role="status" className="alert" style={{ margin: 0 }}>
                {notice}
              </p>
            )}
            <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
              <button type="submit" className="btn btn-primary" disabled={busy || !username || !password}>
                {busy ? '登录中…' : '登录'}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() =>
                  setNotice('当前网站未开放注册，如需账号请直接联系网站管理员。')
                }
              >
                注册
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  )
}
