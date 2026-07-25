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
    <div className="glass" style={{ padding: 20, marginTop: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <strong>{PHASE_LABEL[phase]}</strong>
        <span style={{ color: 'var(--g-text-dim)' }}>
          {progress.chunksDone} / {progress.totalChunks} 块
        </span>
      </div>
      <div
        className="glass-inset"
        style={{ height: 10, marginTop: 12, overflow: 'hidden' }}
      >
        <div
          style={{
            width: `${pct}%`,
            height: '100%',
            background: 'var(--g-accent)',
            transition: 'width 200ms ease',
          }}
        />
      </div>
      <p style={{ color: 'var(--g-text-dim)', fontSize: 14, marginBottom: 0 }}>
        {formatBytes(progress.bytesSent)} / {formatBytes(progress.totalBytes)}
        {remaining !== null && phase === 'uploading'
          ? ` · 约剩 ${Math.ceil(remaining / 1000)} 秒`
          : ''}
      </p>
    </div>
  )
}
