import { useState } from 'react'
import { ApiError, fetchTaskFile, type TaskDto } from '../lib/api'
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

// 解析 Content-Disposition: attachment; filename="foo.pdf" 里的文件名。
// 后端目前只发不带 filename* 的简单形式，但顺手兼容一下引号缺省的情况。
function filenameFromDisposition(disposition: string | null): string | null {
  if (!disposition) return null
  const match = /filename="?([^";]+)"?/i.exec(disposition)
  return match ? match[1] : null
}

export function TaskCard({ taskId }: { taskId: string }) {
  const { task, pollingTimedOut } = useTaskPolling(taskId)
  const [downloadError, setDownloadError] = useState<{ code: string; message: string } | null>(
    null,
  )

  async function handleDownload() {
    if (!task) return
    setDownloadError(null)
    try {
      const resp = await fetchTaskFile(task.task_id)
      const blob = await resp.blob()
      const filename =
        filenameFromDisposition(resp.headers.get('content-disposition')) ??
        `${task.original_filename.replace(/\.[^./]+$/, '')}.pdf`

      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      // 不能同步 revoke：Firefox/Safari 上 click() 返回时下载可能尚未真正
      // 开始，立刻撤销 object URL 会让下载失败。延后 1s 释放。
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (err) {
      if (err instanceof ApiError) {
        setDownloadError({ code: err.code, message: err.message })
      } else {
        setDownloadError({
          code: 'NETWORK_ERROR',
          message: err instanceof Error ? err.message : '下载失败',
        })
      }
    }
  }

  if (pollingTimedOut) {
    return (
      <div className="glass" style={{ padding: 16 }}>
        <p role="alert" style={{ color: 'var(--g-danger)', margin: 0 }}>
          任务状态长时间未更新，可能已中断。请重新上传。
        </p>
      </div>
    )
  }

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
        <button
          type="button"
          className="glass-strong"
          onClick={handleDownload}
          style={{
            display: 'inline-block',
            padding: '8px 20px',
            color: 'var(--g-text)',
            // 不写 border: 'none'——那会盖掉 .glass-strong 的玻璃描边。
            // 项目没有全局 button { font: inherit } 重置，需显式继承字体。
            font: 'inherit',
            cursor: 'pointer',
          }}
        >
          下载 PDF
        </button>
      )}

      {downloadError && (
        <p role="alert" style={{ color: 'var(--g-danger)', fontSize: 14 }}>
          {downloadError.code}：{downloadError.message}
        </p>
      )}
    </div>
  )
}
