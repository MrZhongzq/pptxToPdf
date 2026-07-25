import { useState } from 'react'
import { TaskList } from './components/TaskList'
import { UploadDropzone } from './components/UploadDropzone'
import { UploadProgress } from './components/UploadProgress'
import {
  uploadFile,
  type UploadPhase,
  type UploadProgress as P,
} from './lib/uploadClient'
import './styles/global.css'

const MAX_BYTES = 600 * 1024 * 1024

export default function App() {
  const [taskIds, setTaskIds] = useState<string[]>([])
  const [progress, setProgress] = useState<P | null>(null)
  const [phase, setPhase] = useState<UploadPhase>('done')
  const [error, setError] = useState<string | null>(null)

  const handleFile = async (file: File) => {
    setError(null)
    try {
      const { taskId } = await uploadFile(file, {
        onProgress: setProgress,
        onPhase: setPhase,
      })
      setTaskIds((prev) => [taskId, ...prev])
      setProgress(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="layout">
      <header>
        <h1>pptx → PDF</h1>
        <p style={{ color: 'var(--g-text-dim)' }}>
          一期：上传骨架已通，转换引擎待接入
        </p>
      </header>

      <section>
        <UploadDropzone onFileSelected={handleFile} maxBytes={MAX_BYTES} />
        {progress && <UploadProgress progress={progress} phase={phase} />}
        {error && (
          <p role="alert" style={{ color: 'var(--g-danger)' }}>
            {error}
          </p>
        )}
      </section>

      <section>
        <h2>任务</h2>
        <TaskList taskIds={taskIds} />
      </section>
    </div>
  )
}
