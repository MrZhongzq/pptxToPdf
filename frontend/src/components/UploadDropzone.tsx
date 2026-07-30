import { useRef, useState } from 'react'
import { formatBytes } from '../lib/chunking'

interface Props {
  onFileSelected: (file: File) => void
  maxBytes: number
}

export function UploadDropzone({ onFileSelected, maxBytes }: Props) {
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const accept = (file: File | undefined) => {
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.pptx')) {
      setError('只接受 .pptx 文件')
      return
    }
    if (file.size > maxBytes) {
      setError(`文件 ${formatBytes(file.size)}，超过上限 ${formatBytes(maxBytes)}`)
      return
    }
    setError(null)
    onFileSelected(file)
  }

  return (
    <div>
      <div
        className="card glass"
        style={{
          padding: 'var(--space-7) var(--space-5)',
          textAlign: 'center',
          cursor: 'pointer',
          borderStyle: 'dashed',
          borderWidth: 2,
          borderColor: dragging ? 'var(--c-accent)' : 'var(--c-border-strong)',
          background: dragging ? 'var(--c-accent-soft)' : 'var(--c-bg-elev)',
          transition: 'border-color 140ms ease, background-color 140ms ease',
        }}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          accept(e.dataTransfer.files[0])
        }}
      >
        <p style={{ fontSize: 16, fontWeight: 550 }}>拖入 pptx，或点击选择</p>
        <p
          style={{
            color: 'var(--c-text-dim)',
            fontSize: 13,
            marginTop: 'var(--space-1)',
          }}
        >
          单文件上限 {formatBytes(maxBytes)}
        </p>
        <input
          data-testid="file-input"
          ref={inputRef}
          type="file"
          accept=".pptx"
          hidden
          onChange={(e) => accept(e.target.files?.[0])}
        />
      </div>
      {error && (
        <p
          role="alert"
          className="alert alert-danger"
          style={{ marginTop: 'var(--space-3)' }}
        >
          {error}
        </p>
      )}
    </div>
  )
}
