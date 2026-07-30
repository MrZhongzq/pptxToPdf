import { useEffect, useState } from 'react'

import { useI18n } from '../../i18n'
import { getStats, type SystemStats } from '../../lib/adminApi'

// 状态名与任务卡片共用同一批 key——两处显示的是同一件事，
// 分开维护迟早会出现「面板说已完成、卡片说转换中」这种事。
const STATUS_KEYS: Record<string, string> = {
  pending: 'task.status.pending',
  parsing: 'task.status.parsing',
  queued: 'task.status.queued',
  ready: 'task.status.ready',
  converting: 'task.status.converting',
  merging: 'task.status.merging',
  done: 'task.status.done',
  failed: 'task.status.failed',
}

function mib(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function StatsPanel() {
  const { t } = useI18n()
  const [stats, setStats] = useState<SystemStats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getStats().then(setStats).catch((e: Error) => setError(e.message))
  }, [])

  if (error) {
    return (
      <p role="alert" className="alert alert-danger">
        {error}
      </p>
    )
  }
  if (!stats) return <p style={{ color: 'var(--c-text-dim)' }}>{t('common.loading')}</p>

  const total =
    stats.storage_originals_bytes + stats.storage_outputs_bytes + stats.storage_shards_bytes

  return (
    <>
      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">{t('admin.stats.tasks')}</span>
        <p style={{ margin: 0 }}>{t('admin.stats.tasksTotal', { count: stats.tasks_total })}</p>
        <div style={{ display: 'grid', gap: 'var(--space-1)', marginTop: 'var(--space-2)' }}>
          {Object.entries(stats.tasks_by_status).map(([status, count]) => (
            <div key={status} style={{ display: 'flex', gap: 'var(--space-2)' }}>
              <span style={{ minWidth: 80 }}>{STATUS_KEYS[status] ? t(STATUS_KEYS[status]) : status}</span>
              <strong>{count}</strong>
            </div>
          ))}
        </div>
      </div>

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">{t('admin.stats.storage')}</span>
        <div style={{ display: 'grid', gap: 'var(--space-1)' }}>
          <div>{t('admin.stats.storage.originals')}: {mib(stats.storage_originals_bytes)}</div>
          <div>{t('admin.stats.storage.outputs')}: {mib(stats.storage_outputs_bytes)}</div>
          <div>{t('admin.stats.storage.shards')}: {mib(stats.storage_shards_bytes)}</div>
          <div style={{ marginTop: 'var(--space-2)' }}>
            <strong>{t('admin.stats.storage.total', { size: mib(total) })}</strong>
          </div>
        </div>
      </div>

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">{t('admin.stats.users')}</span>
        <p style={{ margin: 0 }}>{t('admin.stats.usersTotal', { count: stats.users_total })}</p>
      </div>
    </>
  )
}
