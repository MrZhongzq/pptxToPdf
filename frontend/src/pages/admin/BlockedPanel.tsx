import { useEffect, useState, type FormEvent } from 'react'

import {
  createBlocked,
  deleteBlocked,
  listBlocked,
  type AllowedOrigin,
} from '../../lib/adminApi'

/**
 * 网站黑名单。
 *
 * 与白名单的区别必须在界面上说清楚：黑名单**网页与 v1 一起拦**，而且
 * 优先级更高——先查黑名单，命中直接 403，不再进入任何后续判断。不写明
 * 的话，管理员会以为它和白名单是同一层东西。
 */
export function BlockedPanel() {
  const [rows, setRows] = useState<AllowedOrigin[]>([])
  const [origin, setOrigin] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = () => listBlocked().then(setRows).catch((e: Error) => setError(e.message))

  useEffect(() => {
    void reload()
  }, [])

  const handleAdd = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await createBlocked(origin.trim(), note.trim() || undefined)
      setOrigin('')
      setNote('')
      await reload()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div
        className="glass"
        style={{
          padding: 'var(--space-3)',
          borderLeft: '4px solid var(--c-danger)',
          fontSize: 14,
          lineHeight: 1.7,
        }}
      >
        <p>
          黑名单<strong>网页与 v1 一起拦</strong>，命中直接返回 403。
        </p>
        <p style={{ marginTop: 'var(--space-2)' }}>
          优先级<strong>高于白名单</strong>：先查黑名单，命中就直接返回，不再进入
          任何后续判断。只有不在黑名单里的请求才会继续走网页或去匹配白名单。
        </p>
        <p style={{ marginTop: 'var(--space-2)', color: 'var(--c-warn)' }}>
          语法与白名单相同。别把自己的地址加进来——这里没有第二道保险。
        </p>
      </div>

      <form className="card glass" style={{ padding: 'var(--space-4)' }} onSubmit={handleAdd}>
        <span className="section-title">加入黑名单</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          <input
            className="input"
            placeholder="域名或 IP，支持通配与修饰符"
            value={origin}
            onChange={(e) => setOrigin(e.target.value)}
          />
          <input
            className="input"
            placeholder="备注（可选）"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button type="submit" className="btn btn-primary" disabled={busy || !origin.trim()}>
            {busy ? '添加中…' : '加入黑名单'}
          </button>
        </div>
      </form>

      {error && (
        <p role="alert" className="alert alert-danger">
          {error}
        </p>
      )}

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">已封禁（{rows.length}）</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          {rows.map((r) => (
            <div
              key={r.origin_id}
              style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}
            >
              <code>{r.origin}</code>
              {r.note && (
                <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>{r.note}</span>
              )}
              <button
                type="button"
                className="btn btn-ghost"
                style={{ marginLeft: 'auto' }}
                onClick={() =>
                  void deleteBlocked(r.origin_id)
                    .then(reload)
                    .catch((e: Error) => setError(e.message))
                }
              >
                解封
              </button>
            </div>
          ))}
          {rows.length === 0 && (
            <p style={{ color: 'var(--c-text-dim)', margin: 0 }}>黑名单为空</p>
          )}
        </div>
      </div>
    </>
  )
}
