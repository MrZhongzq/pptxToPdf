import { useEffect, useRef, useState } from 'react'
import { estimateRemaining, formatBytes } from '../lib/chunking'
import type { UploadPhase, UploadProgress as P } from '../lib/uploadClient'

const PHASE_LABEL: Record<UploadPhase, string> = {
  creating: '创建会话',
  uploading: '上传中',
  completing: '拼装校验',
  done: '上传完成',
  error: '上传失败',
}

export function UploadProgress({
  progress,
  phase,
}: {
  progress: P
  phase: UploadPhase
}) {
  const started = useRef(Date.now())
  const [, tick] = useState(0)

  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 500)
    return () => clearInterval(id)
  }, [])

  const pct = progress.totalBytes
    ? (progress.bytesSent / progress.totalBytes) * 100
    : 0
  const remaining = estimateRemaining(
    progress.bytesSent,
    progress.totalBytes,
    Date.now() - started.current,
  )

  return (
    <div className="card" style={{ padding: 'var(--space-4)' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 'var(--space-3)',
        }}
      >
        <strong style={{ fontSize: 14 }}>{PHASE_LABEL[phase]}</strong>
        <span style={{ color: 'var(--c-text-dim)', fontSize: 13 }}>
          {progress.chunksDone} / {progress.totalChunks} 块
        </span>
      </div>

      <div
        className="sunken"
        style={{ height: 6, marginTop: 'var(--space-3)', overflow: 'hidden' }}
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          style={{
            width: `${pct}%`,
            height: '100%',
            background: 'var(--c-accent)',
            borderRadius: 999,
            transition: 'width 200ms ease',
          }}
        />
      </div>

      <p
        style={{
          color: 'var(--c-text-dim)',
          fontSize: 13,
          marginTop: 'var(--space-2)',
        }}
      >
        {formatBytes(progress.bytesSent)} / {formatBytes(progress.totalBytes)}
        {remaining !== null && phase === 'uploading'
          ? ` · 约剩 ${Math.ceil(remaining / 1000)} 秒`
          : ''}
      </p>
    </div>
  )
}
