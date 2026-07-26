import { useState } from 'react'
import { ConversionOptionsPanel } from './components/ConversionOptions'
import { TaskList } from './components/TaskList'
import { UploadDropzone } from './components/UploadDropzone'
import { UploadProgress } from './components/UploadProgress'
import {
  DEFAULT_OPTIONS,
  type ConversionOptions,
  type EngineName,
} from './lib/api'
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
  const [engine, setEngine] = useState<EngineName>('libreoffice')
  const [options, setOptions] = useState<ConversionOptions>(DEFAULT_OPTIONS)

  const uploading = progress !== null

  const handleFile = async (file: File) => {
    setError(null)
    try {
      const { taskId } = await uploadFile(file, {
        engine,
        options,
        onProgress: setProgress,
        onPhase: setPhase,
      })
      setTaskIds((prev) => [taskId, ...prev])
      setProgress(null)
    } catch (err) {
      setProgress(null)
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="layout">
      <header className="page-head">
        <h1 className="page-title">pptx → PDF</h1>
        <span className="page-sub">
          转成能直接导入 GoodNotes / OneNote 的 PDF
        </span>
      </header>

      <div className="col">
        <UploadDropzone onFileSelected={handleFile} maxBytes={MAX_BYTES} />

        {progress && <UploadProgress progress={progress} phase={phase} />}

        {error && (
          <p role="alert" className="alert alert-danger">
            {error}
          </p>
        )}

        <ConversionOptionsPanel
          engine={engine}
          onEngineChange={setEngine}
          options={options}
          onOptionsChange={setOptions}
          disabled={uploading}
        />
      </div>

      <div className="col">
        <span className="section-title">任务</span>
        <TaskList taskIds={taskIds} />
      </div>
    </div>
  )
}
