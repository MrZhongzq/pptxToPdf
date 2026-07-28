import { useState } from 'react'
import {
  ApiError,
  preflightDownload,
  triggerNativeDownload,
  type TaskDto,
} from '../lib/api'
import { formatBytes } from '../lib/chunking'
import { useTaskPolling } from '../hooks/useTaskPolling'

// 必须覆盖 TaskDto['status'] 的每一个取值：后端的 status 是裸 str、API 原样
// 吐出，这张表是唯一的兜底。少一个键就是 `STATUS[x].badge` 读 undefined，
// 而仓库里没有 ErrorBoundary，React 未捕获的渲染异常会卸载整棵树。
// Record<联合类型, …> 让漏键在 tsc 阶段就报错，TaskCard.test.tsx 再守一层运行时。
const STATUS: Record<TaskDto['status'], { label: string; badge: string }> = {
  pending: { label: '排队中', badge: 'badge-neutral' },
  parsing: { label: '解析中', badge: 'badge-accent' },
  queued: { label: '等待转换', badge: 'badge-neutral' },
  converting: { label: '转换中', badge: 'badge-accent' },
  // 三期分片路径专有：N 片各自转完之后合并回一份 PDF。
  merging: { label: '合并中', badge: 'badge-accent' },
  done: { label: '完成', badge: 'badge-success' },
  failed: { label: '失败', badge: 'badge-danger' },
}

const ENGINE_LABEL: Record<string, string> = {
  libreoffice: 'LibreOffice',
  graph: 'Microsoft Graph',
  placeholder: '占位引擎',
  unassigned: '待分配',
}

const EMU_PER_INCH = 914400

const OPTION_LABEL: Record<string, string> = {
  expand_animations: '动画分步',
  pdf_outline: '书签大纲',
  remap_margins: '页边距重映射',
}

export function TaskCard({ taskId }: { taskId: string }) {
  const { task, pollingTimedOut } = useTaskPolling(taskId)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)

  async function handleDownload() {
    if (!task) return
    setDownloadError(null)
    setChecking(true)
    try {
      await preflightDownload(task.task_id)
      triggerNativeDownload(task.task_id)
    } catch (err) {
      setDownloadError(
        err instanceof ApiError
          ? `${err.code}：${err.message}`
          : err instanceof Error
            ? err.message
            : '下载失败',
      )
    } finally {
      setChecking(false)
    }
  }

  if (pollingTimedOut) {
    return (
      <div className="card" style={{ padding: 'var(--space-4)' }}>
        <p role="alert" className="alert alert-danger">
          任务状态长时间未更新，可能已中断。请重新上传。
        </p>
      </div>
    )
  }

  if (!task) {
    return (
      <div
        className="card"
        style={{ padding: 'var(--space-4)', color: 'var(--c-text-dim)' }}
      >
        载入中…
      </div>
    )
  }

  const status = STATUS[task.status]
  const size =
    task.slide_width_emu && task.slide_height_emu
      ? `${(task.slide_width_emu / EMU_PER_INCH).toFixed(1)}″ × ${(
          task.slide_height_emu / EMU_PER_INCH
        ).toFixed(1)}″`
      : null
  const enabledOptions = Object.entries(task.options ?? {})
    .filter(([, on]) => on)
    .map(([k]) => OPTION_LABEL[k] ?? k)
  const inFlight = !['done', 'failed'].includes(task.status)
  // 分片任务：从一开始就要在 UX 上特殊显示，不能和普通任务长得一模一样
  // 让用户干等——用户原话「只要不是静默让用户等 10 分钟就行」。
  const sharded = task.shard_total !== null && task.shard_total > 0
  const shardPct = sharded
    ? Math.round((task.shard_done / (task.shard_total as number)) * 100)
    : 0

  return (
    <div
      className="card"
      style={{
        padding: 'var(--space-4)',
        ...(sharded ? { borderLeft: '4px solid var(--c-notable)' } : null),
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 'var(--space-3)',
        }}
      >
        <strong style={{ wordBreak: 'break-word', lineHeight: 1.4 }}>
          {task.original_filename}
        </strong>
        <span className={`badge ${status.badge}`}>{status.label}</span>
      </div>

      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: '0 var(--space-3)',
          marginTop: 'var(--space-2)',
          fontSize: 13,
          color: 'var(--c-text-dim)',
        }}
      >
        <span>{formatBytes(task.size_bytes)}</span>
        {task.slide_count !== null && <span>{task.slide_count} 页</span>}
        {size && <span>{size}</span>}
        {task.engine !== 'unassigned' && (
          <span>{ENGINE_LABEL[task.engine] ?? task.engine}</span>
        )}
      </div>

      {enabledOptions.length > 0 && (
        <div
          style={{
            display: 'flex',
            flexWrap: 'wrap',
            gap: 'var(--space-2)',
            marginTop: 'var(--space-3)',
          }}
        >
          {enabledOptions.map((o) => (
            <span key={o} className="badge badge-neutral">
              {o}
            </span>
          ))}
        </div>
      )}

      {inFlight && (
        <>
          {sharded && (
            <p
              style={{
                fontSize: 13,
                color: 'var(--c-notable)',
                marginTop: 'var(--space-3)',
              }}
            >
              已完成 {task.shard_done} / {task.shard_total} 片
            </p>
          )}
          <div
            className="sunken"
            style={{
              height: 4,
              marginTop: 'var(--space-2)',
              overflow: 'hidden',
            }}
          >
            <div
              style={
                sharded
                  ? {
                      width: `${shardPct}%`,
                      height: '100%',
                      background: 'var(--c-notable)',
                      borderRadius: 999,
                      transition: 'width 300ms ease',
                    }
                  : {
                      width: '35%',
                      height: '100%',
                      background: 'var(--c-accent)',
                      borderRadius: 999,
                      animation: 'indeterminate 1.4s ease-in-out infinite',
                    }
              }
            />
          </div>
        </>
      )}

      {task.status === 'failed' && (
        <p
          role="alert"
          className="alert alert-danger"
          style={{ marginTop: 'var(--space-3)' }}
        >
          {task.error_code}：{task.error_message}
        </p>
      )}

      {task.status === 'done' && (
        <button
          type="button"
          className="btn btn-primary"
          onClick={handleDownload}
          disabled={checking}
          style={{ marginTop: 'var(--space-3)' }}
        >
          {checking ? '准备中…' : '下载 PDF'}
        </button>
      )}

      {downloadError && (
        <p
          role="alert"
          className="alert alert-danger"
          style={{ marginTop: 'var(--space-3)' }}
        >
          {downloadError}
        </p>
      )}
    </div>
  )
}
