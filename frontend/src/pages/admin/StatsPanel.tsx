import { useEffect, useState } from 'react'

import { getStats, type SystemStats } from '../../lib/adminApi'

const STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  parsing: '解析中',
  queued: '已入队',
  ready: '待开始',
  converting: '转换中',
  merging: '合并中',
  done: '已完成',
  failed: '失败',
}

function mib(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function StatsPanel() {
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
  if (!stats) return <p style={{ color: 'var(--c-text-muted)' }}>加载中…</p>

  const total =
    stats.storage_originals_bytes + stats.storage_outputs_bytes + stats.storage_shards_bytes

  return (
    <>
      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">任务</span>
        <p style={{ margin: 0 }}>共 {stats.tasks_total} 个</p>
        <div style={{ display: 'grid', gap: 'var(--space-1)', marginTop: 'var(--space-2)' }}>
          {Object.entries(stats.tasks_by_status).map(([status, count]) => (
            <div key={status} style={{ display: 'flex', gap: 'var(--space-2)' }}>
              <span style={{ minWidth: 80 }}>{STATUS_LABELS[status] ?? status}</span>
              <strong>{count}</strong>
            </div>
          ))}
        </div>
      </div>

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">存储占用</span>
        <div style={{ display: 'grid', gap: 'var(--space-1)' }}>
          <div>原始 pptx：{mib(stats.storage_originals_bytes)}</div>
          <div>输出 PDF：{mib(stats.storage_outputs_bytes)}</div>
          <div>分片中间产物：{mib(stats.storage_shards_bytes)}</div>
          <div style={{ marginTop: 'var(--space-2)' }}>
            <strong>合计 {mib(total)}</strong>
          </div>
        </div>
      </div>

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">账号</span>
        <p style={{ margin: 0 }}>共 {stats.users_total} 个</p>
      </div>
    </>
  )
}
