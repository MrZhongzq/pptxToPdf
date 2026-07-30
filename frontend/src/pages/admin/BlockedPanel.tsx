import { useEffect, useState, type FormEvent } from 'react'

import { useI18n } from '../../i18n'
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
  const { t } = useI18n()
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
        <p>{t('admin.blocked.scope')}</p>
        <p style={{ marginTop: 'var(--space-2)' }}>{t('admin.blocked.priority')}</p>
        <p style={{ marginTop: 'var(--space-2)', color: 'var(--c-warn)' }}>
          {t('admin.blocked.warning')}
        </p>
      </div>

      <form className="card glass" style={{ padding: 'var(--space-4)' }} onSubmit={handleAdd}>
        <span className="section-title">{t('admin.blocked.add')}</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          <input
            className="input"
            placeholder={t('admin.blocked.placeholder')}
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
            {busy ? t('common.adding') : t('admin.blocked.add')}
          </button>
        </div>
      </form>

      {error && (
        <p role="alert" className="alert alert-danger">
          {error}
        </p>
      )}

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">{t('admin.blocked.list', { count: rows.length })}</span>
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
                {t('admin.blocked.unblock')}
              </button>
            </div>
          ))}
          {rows.length === 0 && (
            <p style={{ color: 'var(--c-text-dim)', margin: 0 }}>{t('admin.blocked.empty')}</p>
          )}
        </div>
      </div>
    </>
  )
}
