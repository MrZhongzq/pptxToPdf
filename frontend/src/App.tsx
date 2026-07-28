import { useEffect, useState } from 'react'
import { ConversionOptionsPanel } from './components/ConversionOptions'
import { TaskList } from './components/TaskList'
import { UploadDropzone } from './components/UploadDropzone'
import { UploadProgress } from './components/UploadProgress'
import {
  DEFAULT_OPTIONS,
  getCapacityConfig,
  type CapacityConfig,
  type ConversionOptions,
  type EngineName,
} from './lib/api'
import { assessGraphRisk, type GraphRisk } from './lib/graphCapacity'
import {
  uploadFile,
  type UploadPhase,
  type UploadProgress as P,
} from './lib/uploadClient'
import './styles/global.css'

const MAX_BYTES = 600 * 1024 * 1024

const GRAPH_RISK_MESSAGE: Record<Exclude<GraphRisk, 'none'>, string> = {
  shard:
    '此文件较大，Graph 通道会将其切分后分批转换，耗时可能达到十几分钟。改用 LibreOffice 通常在一分钟内完成。',
  budget:
    '此文件体积较大，即使 Graph 通道顺利切片、逐片转换成功，仍可能在最后合并阶段因总体积超限而失败——这种失败发生在转换即将完成时，最费时间。' +
    'PDF 实际体积与 pptx 不成固定比例，无法精确预测，但体积越大风险越高。建议改用 LibreOffice 引擎。',
}

export default function App() {
  const [taskIds, setTaskIds] = useState<string[]>([])
  const [progress, setProgress] = useState<P | null>(null)
  const [phase, setPhase] = useState<UploadPhase>('done')
  const [error, setError] = useState<string | null>(null)
  const [engine, setEngine] = useState<EngineName>('libreoffice')
  const [options, setOptions] = useState<ConversionOptions>(DEFAULT_OPTIONS)
  const [pendingFile, setPendingFile] = useState<File | null>(null)
  // 容量数字只能来自后端 settings（四期上真实租户后可能被回调），前端不
  // 硬编码一份可能漂移的副本；取到之前不渲染任何容量相关的提示。
  const [capacity, setCapacity] = useState<CapacityConfig | null>(null)

  useEffect(() => {
    let cancelled = false
    getCapacityConfig()
      .then((c) => {
        if (!cancelled) setCapacity(c)
      })
      .catch(() => {
        // 取不到就不提示——总比拿一份陈旧/瞎猜的数字误导用户强。
      })
    return () => {
      cancelled = true
    }
  }, [])

  const uploading = progress !== null

  const graphRisk: GraphRisk =
    engine === 'graph' && pendingFile !== null && capacity !== null
      ? assessGraphRisk(pendingFile.size, capacity)
      : 'none'

  const handleFile = async (file: File) => {
    setError(null)
    setPendingFile(file)
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
    } finally {
      setPendingFile(null)
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

        {graphRisk !== 'none' && (
          <div
            className="card"
            style={{
              padding: 'var(--space-3)',
              borderLeft: '4px solid var(--c-notable)',
              fontSize: 13,
              lineHeight: 1.6,
            }}
          >
            {GRAPH_RISK_MESSAGE[graphRisk]}
          </div>
        )}

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
