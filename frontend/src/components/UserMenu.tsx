import { useEffect, useRef, useState, type FormEvent } from 'react'

import { useI18n } from '../i18n'
import { ApiError, getMe, login, logout, type UserDto } from '../lib/api'

/**
 * 右上角的用户入口。
 *
 * 未登录显示「登录」；登录后显示用户名，admin 额外多一个「admin 面板」
 * 按钮。注册按钮存在但点了只弹一句说明——站点当前不开放注册，后端
 * 刻意没有注册端点（留一个关着的端点只是凭空多一个攻击面）。
 */
export function UserMenu({ onUserChange }: { onUserChange?: (u: UserDto | null) => void }) {
  const { t } = useI18n()
  const [user, setUser] = useState<UserDto | null>(null)
  const [open, setOpen] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const firstFieldRef = useRef<HTMLInputElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)

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

  // Esc 关闭。点遮罩关闭由 .glass-overlay 的 onMouseDown 承担——不用
  // document 级监听，那样容易和「点触发按钮」互相打架（点开的同一次
  // 点击会冒泡到 document 上，立刻又把它关掉）。
  //
  // 不用原生 <dialog>：它的 backdrop 在 Safari 上表现不一致，而这里的
  // 结构很简单，自己管更省心。
  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  // 打开时把焦点移进模态，关闭后还给触发按钮——键盘用户不该被留在
  // 一个已经消失的元素上。
  useEffect(() => {
    if (open) {
      firstFieldRef.current?.focus()
    } else {
      triggerRef.current?.focus?.()
    }
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
      setError(err instanceof ApiError ? err.message : t('auth.failed'))
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
          {t('nav.adminPanel')}
        </a>
      )}

      {user ? (
        <>
          <span style={{ fontSize: 13, color: 'var(--c-text-muted)' }}>{user.username}</span>
          <button type="button" className="btn btn-ghost" onClick={handleLogout}>
            {t('nav.logout')}
          </button>
        </>
      ) : (
        <button
          ref={triggerRef}
          type="button"
          className="btn btn-ghost"
          aria-expanded={open}
          aria-haspopup="dialog"
          onClick={() => setOpen((v) => !v)}
        >
          {/* 与弹窗里的提交按钮区分开：两个可访问名相同会让屏幕阅读器
              用户分不清「打开面板」和「提交表单」。文案也更贴合需求
              原文的「登录/注册」。 */}
          {t('nav.login')}
        </button>
      )}

      {open && !user && (
        <div
          className="glass-overlay"
          onMouseDown={(e) => {
            // 只有点在遮罩本身（而不是冒泡上来的模态内部）才关闭
            if (e.target === e.currentTarget) setOpen(false)
          }}
        >
        <div
          ref={dialogRef}
          className="glass-strong glass-modal"
          role="dialog"
          aria-modal="true"
          aria-label={t('auth.dialogTitle')}
        >
          <form onSubmit={handleLogin} style={{ display: 'grid', gap: 'var(--space-3)' }}>
            <span className="section-title">{t('auth.dialogTitle')}</span>
            <input
              ref={firstFieldRef}
              className="input"
              placeholder={t('auth.username')}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              disabled={busy}
            />
            <input
              className="input"
              type="password"
              placeholder={t('auth.password')}
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
                {busy ? t('auth.submitting') : t('auth.submit')}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() =>
                  setNotice(t('auth.registerClosed'))
                }
              >
                {t('auth.register')}
              </button>
            </div>
          </form>
        </div>
        </div>
      )}
    </div>
  )
}
