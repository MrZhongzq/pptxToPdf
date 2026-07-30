import { useEffect, useState, type FormEvent } from 'react'

import { useI18n } from '../../i18n'
import { createOrigin, deleteOrigin, listOrigins, type AllowedOrigin } from '../../lib/adminApi'

/** 语法说明的每一行：左边是例子（不翻译），右边是解释（翻译）。 */
const SYNTAX_ROWS: { sample: string; key: string }[] = [
  { sample: 'example.com', key: 'admin.origins.syntax.exact' },
  { sample: '*.example.com', key: 'admin.origins.syntax.wildcard' },
  { sample: '203.0.113.7', key: 'admin.origins.syntax.ip' },
  { sample: '*.a.com||@except{x.a.com}', key: 'admin.origins.syntax.except' },
  { sample: '*.a.com||@match{api.a.com}', key: 'admin.origins.syntax.match' },
  { sample: 'a.com||@no_graph', key: 'admin.origins.syntax.noGraph' },
]

/**
 * 访问白名单。
 *
 * 面板上必须把作用域说清楚：它**只管 v1**，网页永不受影响。不写明的话，
 * 管理员会以为配错就把整站关掉了，从而根本不敢碰它。
 */
export function OriginsPanel() {
  const { t } = useI18n()
  const [rows, setRows] = useState<AllowedOrigin[]>([])
  const [origin, setOrigin] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = () => listOrigins().then(setRows).catch((e: Error) => setError(e.message))

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
        <p>{t('admin.origins.scope', { path: '/v1/convert' })}</p>
        <p style={{ marginTop: 'var(--space-2)' }}>
          <strong>{t('admin.origins.emptyMeans')}</strong>
        </p>
        <p style={{ marginTop: 'var(--space-2)', color: 'var(--c-warn)' }}>
          {t('admin.origins.lockoutWarning')}
        </p>
      </div>

      <div className="glass" style={{ padding: 'var(--space-3)', fontSize: 13, lineHeight: 1.8 }}>
        <strong>{t('admin.origins.syntax')}</strong>
        <ul style={{ margin: 'var(--space-2) 0 0', paddingLeft: '1.2em' }}>
          {SYNTAX_ROWS.map((row) => (
            <li key={row.sample}>
              <code>{row.sample}</code> — {t(row.key)}
            </li>
          ))}
        </ul>
      </div>

      <form className="card glass" style={{ padding: 'var(--space-4)' }} onSubmit={handleAdd}>
        <span className="section-title">{t('admin.origins.add')}</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          <input
            className="input"
            placeholder={t('admin.origins.placeholder')}
            value={origin}
            onChange={(e) => setOrigin(e.target.value)}
          />
          <input
            className="input"
            placeholder={t('common.note')}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button type="submit" className="btn btn-primary" disabled={busy || !origin.trim()}>
            {busy ? t('common.adding') : t('common.add')}
          </button>
        </div>
      </form>

      {error && (
        <p role="alert" className="alert alert-danger">
          {error}
        </p>
      )}

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">{t('admin.origins.list', { count: rows.length })}</span>
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
                  void deleteOrigin(r.origin_id)
                    .then(reload)
                    .catch((e: Error) => setError(e.message))
                }
              >
                {t('common.delete')}
              </button>
            </div>
          ))}
          {rows.length === 0 && (
            <p style={{ color: 'var(--c-text-dim)', margin: 0 }}>{t('admin.origins.empty')}</p>
          )}
        </div>
      </div>
    </>
  )
}
