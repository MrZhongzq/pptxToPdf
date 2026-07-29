import { useEffect, useState, type FormEvent } from 'react'

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
      <form className="card" style={{ padding: 'var(--space-4)' }} onSubmit={handleCreate}>
        <span className="section-title">添加账号</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          <input
            className="input"
            placeholder="用户名（唯一，3-32 位字母数字与 _ -）"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
          <input
            className="input"
            placeholder="邮箱"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <input
            className="input"
            type="password"
            placeholder="密码（至少 8 位）"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <select
            className="input"
            value={role}
            onChange={(e) => setRole(e.target.value as 'admin' | 'user')}
            aria-label="角色"
          >
            <option value="user">普通用户</option>
            <option value="admin">管理员</option>
          </select>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={creating || !username || !email || !password}
          >
            {creating ? '创建中…' : '创建'}
          </button>
        </div>
      </form>

      {error && (
        <p role="alert" className="alert alert-danger">
          {error}
        </p>
      )}

      <div className="card" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">账号列表（{users.length}）</span>
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
                {u.role === 'admin' ? '管理员' : '用户'}
              </span>
              <span className={u.status === 'active' ? 'badge badge-ok' : 'badge badge-warn'}>
                {u.status === 'active' ? '正常' : '已暂停'}
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
                      {u.status === 'active' ? '暂停' : '激活'}
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      disabled={busy === u.user_id}
                      onClick={() => {
                        // 删除不可逆，且这个系统没有回收站
                        if (!window.confirm(`确定删除账号「${u.username}」？此操作不可撤销。`)) return
                        void act(u.user_id, () => deleteUser(u.user_id))
                      }}
                    >
                      删除
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={busy === u.user_id}
                  onClick={() => {
                    const pw = window.prompt(`给「${u.username}」设置新密码（至少 8 位）`)
                    if (!pw) return
                    void act(u.user_id, () => setUserPassword(u.user_id, pw))
                  }}
                >
                  改密码
                </button>
              </span>
            </div>
          ))}
          {users.length === 0 && (
            <p style={{ color: 'var(--c-text-muted)', margin: 0 }}>还没有账号</p>
          )}
        </div>
      </div>
    </>
  )
}
