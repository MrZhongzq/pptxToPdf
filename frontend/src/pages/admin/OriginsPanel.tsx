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
        className="glass"
        style={{
          padding: 'var(--space-3)',
          borderLeft: '4px solid var(--c-notable)',
          fontSize: 14,
          lineHeight: 1.7,
        }}
      >
        <p>
          白名单<strong>只作用于 HTTP v1 接口</strong>（<code>/v1/convert</code>）。
          网页永远不受它影响——即使这里一条都不填，webui 照常使用。
        </p>
        <p style={{ marginTop: 'var(--space-2)' }}>
          <strong>空白名单 = v1 谁也不许用。</strong>
        </p>
        <p style={{ marginTop: 'var(--space-2)', color: 'var(--c-warn)' }}>
          记得把自己解析域名加进来，避免把自己关在门外。
        </p>
      </div>

      <div className="glass" style={{ padding: 'var(--space-3)', fontSize: 13, lineHeight: 1.8 }}>
        <strong>支持的语法</strong>
        <ul style={{ margin: 'var(--space-2) 0 0', paddingLeft: '1.2em' }}>
          <li>
            <code>example.com</code> — 精确匹配
          </li>
          <li>
            <code>*.example.com</code> — 任意子域（不含裸域本身）
          </li>
          <li>
            <code>203.0.113.7</code> — IP
          </li>
          <li>
            <code>{'*.a.com||@except{x.a.com}'}</code> — 通配但排除
          </li>
          <li>
            <code>{'*.a.com||@match{api.a.com}'}</code> — 范围内只允许列出的
          </li>
          <li>
            <code>{'a.com||@no_graph'}</code> — 该来源不得使用 graph 引擎
          </li>
        </ul>
      </div>

      <form className="card glass" style={{ padding: 'var(--space-4)' }} onSubmit={handleAdd}>
        <span className="section-title">添加白名单</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          <input
            className="input"
            placeholder={'例如 *.example.com||@except{a.example.com}'}
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

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
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
            <p style={{ color: 'var(--c-text-dim)', margin: 0 }}>白名单为空 —— v1 当前不可用</p>
          )}
        </div>
      </div>
    </>
  )
}
