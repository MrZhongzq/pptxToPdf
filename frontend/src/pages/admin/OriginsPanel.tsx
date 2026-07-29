import { useEffect, useState, type FormEvent } from 'react'

import { createOrigin, deleteOrigin, listOrigins, type AllowedOrigin } from '../../lib/adminApi'

/**
 * 访问白名单。
 *
 * 这个功能目前**默认关闭**（后端 origin_guard_enabled 默认 false），
 * 面板上必须把这件事说清楚——否则管理员配了一堆条目却发现没有任何效果，
 * 只会以为是坏的。
 */
export function OriginsPanel() {
  const [rows, setRows] = useState<AllowedOrigin[]>([])
  const [origin, setOrigin] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = () => listOrigins().then(setRows).catch((e) => setError(e.message))

  useEffect(() => {
    void reload()
  }, [])

  const handleAdd = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await createOrigin(origin.trim(), note.trim() || undefined)
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
        className="card"
        style={{
          padding: 'var(--space-3)',
          borderLeft: '4px solid var(--c-notable)',
          fontSize: 13,
          lineHeight: 1.6,
        }}
      >
        防跨站保护当前<strong>未启用</strong>（需要把后端的
        <code> PPTX2PDF_ORIGIN_GUARD_ENABLED </code>
        设为 true）。启用后，只有列在这里的来源才能发起写请求；白名单为空时
        即使启用也一律放行——这是防止把自己关在门外的第二道保险。
      </div>

      <form className="card" style={{ padding: 'var(--space-4)' }} onSubmit={handleAdd}>
        <span className="section-title">添加白名单</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          <input
            className="input"
            placeholder="域名或 IP，可带端口（不要带 http:// 与路径）"
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
            {busy ? '添加中…' : '添加'}
          </button>
        </div>
      </form>

      {error && (
        <p role="alert" className="alert alert-danger">
          {error}
        </p>
      )}

      <div className="card" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">已允许的来源（{rows.length}）</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          {rows.map((r) => (
            <div key={r.origin_id} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
              <code>{r.origin}</code>
              {r.note && (
                <span style={{ fontSize: 12, color: 'var(--c-text-muted)' }}>{r.note}</span>
              )}
              <button
                type="button"
                className="btn btn-ghost"
                style={{ marginLeft: 'auto' }}
                onClick={() =>
                  void deleteOrigin(r.origin_id)
                    .then(reload)
                    .catch((e: Error) => setError(e.message))
                }
              >
                删除
              </button>
            </div>
          ))}
          {rows.length === 0 && (
            <p style={{ color: 'var(--c-text-muted)', margin: 0 }}>白名单为空（当前一律放行）</p>
          )}
        </div>
      </div>
    </>
  )
}
