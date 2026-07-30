import { useEffect, useState, type FormEvent } from 'react'

import { useI18n } from '../../i18n'
import type { UserDto } from '../../lib/api'
import {
  createUser,
  deleteUser,
  listUsers,
  setUserPassword,
  setUserStatus,
} from '../../lib/adminApi'

/** 账号的三个要素：用户名、邮箱、密码。用户名唯一且不可重复。 */
export function UsersPanel({ currentUserId }: { currentUserId: string }) {
  const { t } = useI18n()
  const [users, setUsers] = useState<UserDto[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<'admin' | 'user'>('user')
  const [creating, setCreating] = useState(false)

  const reload = () => listUsers().then(setUsers).catch((e) => setError(e.message))

  useEffect(() => {
    void reload()
  }, [])

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    setCreating(true)
    setError(null)
    try {
      await createUser({ username: username.trim(), email: email.trim(), password, role })
      setUsername('')
      setEmail('')
      setPassword('')
      setRole('user')
      await reload()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setCreating(false)
    }
  }

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id)
    setError(null)
    try {
      await fn()
      await reload()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <form className="card glass" style={{ padding: 'var(--space-4)' }} onSubmit={handleCreate}>
        <span className="section-title">{t('admin.users.add')}</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          <input
            className="input"
            placeholder={t('admin.users.username')}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
          <input
            className="input"
            placeholder={t('admin.users.email')}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <input
            className="input"
            type="password"
            placeholder={t('admin.users.password')}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <select
            className="input"
            value={role}
            onChange={(e) => setRole(e.target.value as 'admin' | 'user')}
            aria-label={t('admin.users.role')}
          >
            <option value="user">{t('admin.users.role.user')}</option>
            <option value="admin">{t('admin.users.role.admin')}</option>
          </select>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={creating || !username || !email || !password}
          >
            {creating ? t('admin.users.creating') : t('admin.users.create')}
          </button>
        </div>
      </form>

      {error && (
        <p role="alert" className="alert alert-danger">
          {error}
        </p>
      )}

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">{t('admin.users.list', { count: users.length })}</span>
        <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
          {users.map((u) => (
            <div
              key={u.user_id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 'var(--space-2)',
                flexWrap: 'wrap',
                paddingBottom: 'var(--space-2)',
                borderBottom: '1px solid var(--c-border)',
              }}
            >
              <strong>{u.username}</strong>
              <span style={{ fontSize: 12, color: 'var(--c-text-muted)' }}>{u.email}</span>
              <span className={u.role === 'admin' ? 'badge badge-notable' : 'badge badge-neutral'}>
                {u.role === 'admin' ? t('admin.users.badge.admin') : t('admin.users.badge.user')}
              </span>
              <span className={u.status === 'active' ? 'badge badge-ok' : 'badge badge-warn'}>
                {u.status === 'active' ? t('admin.users.status.active') : t('admin.users.status.suspended')}
              </span>

              <span style={{ marginLeft: 'auto', display: 'flex', gap: 'var(--space-2)' }}>
                {u.user_id !== currentUserId && (
                  <>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      disabled={busy === u.user_id}
                      onClick={() =>
                        void act(u.user_id, () =>
                          setUserStatus(u.user_id, u.status === 'active' ? 'suspended' : 'active'),
                        )
                      }
                    >
                      {u.status === 'active' ? t('admin.users.suspend') : t('admin.users.activate')}
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      disabled={busy === u.user_id}
                      onClick={() => {
                        // 删除不可逆，且这个系统没有回收站
                        if (!window.confirm(t('admin.users.confirmDelete', { username: u.username }))) return
                        void act(u.user_id, () => deleteUser(u.user_id))
                      }}
                    >
                      {t('common.delete')}
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={busy === u.user_id}
                  onClick={() => {
                    const pw = window.prompt(t('admin.users.promptPassword', { username: u.username }))
                    if (!pw) return
                    void act(u.user_id, () => setUserPassword(u.user_id, pw))
                  }}
                >
                  {t('admin.users.changePassword')}
                </button>
              </span>
            </div>
          ))}
          {users.length === 0 && (
            <p style={{ color: 'var(--c-text-dim)', margin: 0 }}>{t('admin.users.empty')}</p>
          )}
        </div>
      </div>
    </>
  )
}
