import { downloadUrl, type TaskDto } from '../lib/api'
import { formatBytes } from '../lib/chunking'
import { useTaskPolling } from '../hooks/useTaskPolling'

const STATUS_LABEL: Record<TaskDto['status'], string> = {
  pending: '排队中',
  parsing: '解析中',
  queued: '等待转换',
  converting: '转换中',
  done: '完成',
  failed: '失败',
}

const EMU_PER_INCH = 914400

export function TaskCard({ taskId }: { taskId: string }) {
  const task = useTaskPolling(taskId)

  if (!task) {
    return (
      <div className="glass" style={{ padding: 16 }}>
        载入中…
      </div>
    )
  }

  const ratio =
    task.slide_width_emu && task.slide_height_emu
      ? (task.slide_width_emu / EMU_PER_INCH).toFixed(1) +
        '″ × ' +
        (task.slide_height_emu / EMU_PER_INCH).toFixed(1) +
        '″'
      : null

  return (
    <div className="glass" style={{ padding: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
        <strong style={{ wordBreak: 'break-all' }}>
          {task.original_filename}
        </strong>
        <span
          style={{
            color:
              task.status === 'failed'
                ? 'var(--g-danger)'
                : task.status === 'done'
                  ? 'var(--g-success)'
                  : 'var(--g-text-dim)',
            whiteSpace: 'nowrap',
          }}
        >
          {STATUS_LABEL[task.status]}
        </span>
      </div>

      <p style={{ color: 'var(--g-text-dim)', fontSize: 14 }}>
        {formatBytes(task.size_bytes)}
        {task.slide_count !== null ? ` · ${task.slide_count} 页` : ''}
        {ratio ? ` · ${ratio}` : ''}
      </p>

      {task.status === 'failed' && (
        <p role="alert" style={{ color: 'var(--g-danger)', fontSize: 14 }}>
          {task.error_code}：{task.error_message}
        </p>
      )}

      {task.status === 'done' && (
        <a
          className="glass-strong"
          href={downloadUrl(task.task_id)}
          style={{
            display: 'inline-block',
            padding: '8px 20px',
            color: 'var(--g-text)',
            textDecoration: 'none',
          }}
        >
          下载 PDF
        </a>
      )}
    </div>
  )
}
