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
import { AdminPage } from './pages/AdminPage'
import './styles/global.css'

// 取不到 /api/config/capacity（网络失败、还没返回）时的兜底——只用于
// UploadDropzone 的前端早退拦截，真正的硬上限判定始终在后端。
const MAX_BYTES = 600 * 1024 * 1024

// 三档措辞对应三种不同的失败/延迟形态（见 lib/graphCapacity.ts 的文档
// 注释），审查后特别注意两点：
// 1. 不写绝对时长/百分比——LibreOffice 通道的真实耗时由
//    convert_timeout_base_s / per_slide_s / per_mb_s 决定，仓库里没有
//    支撑"一分钟内"这类承诺的实测数据；写死的数字一旦不成立，是在
//    透支这个功能本身的可信度。
// 2. "合并阶段"与"规划阶段"是两码事，不能混用——budget 档失败在合并，
//    reject 档失败在规划，用户看到哪种措辞决定了他对失败时机的预期。
const GRAPH_RISK_MESSAGE: Record<Exclude<GraphRisk, 'none'>, string> = {
  shard:
    '此文件较大，Graph 通道会将其切分后分批转换，比不切片更慢。' +
    'LibreOffice 通道不切片，通常明显更快。',
  budget:
    '此文件体积较大，即使 Graph 通道顺利切片、逐片转换成功，仍可能在最终合并阶段因总体积超限而失败——' +
    '这种失败发生在转换即将完成时，最费时间。PDF 实际体积与 pptx 不成固定比例，无法精确预测，但体积越大风险越高。' +
    '建议改用 LibreOffice 引擎。',
  reject:
    '此文件已超过 Graph 通道能处理的分片总容量，大概率会在切片规划阶段就被直接拒绝，不会先切片再浪费转换时间。' +
    '建议改用 LibreOffice 引擎。',
}

export default function App() {
  // 只有两个页面，一个 pathname 判断足够——不引入 react-router 这个依赖
  if (typeof window !== 'undefined' && window.location.pathname.startsWith('/admin')) {
    return <AdminPage />
  }

  const [taskIds, setTaskIds] = useState<string[]>([])
  const [progress, setProgress] = useState<P | null>(null)
  const [phase, setPhase] = useState<UploadPhase>('done')
  const [error, setError] = useState<string | null>(null)
  const [engine, setEngine] = useState<EngineName>('libreoffice')
  const [options, setOptions] = useState<ConversionOptions>(DEFAULT_OPTIONS)
  // 选中但还没决定要不要传的文件——只有命中风险时才会停在这一步；
  // 没有风险时直接进 startUpload，不占用这个 state。
  const [pendingFile, setPendingFile] = useState<File | null>(null)
  // 容量数字只能来自后端 settings（四期上真实租户后可能被回调），前端不
  // 硬编码一份可能漂移的副本；取到之前不渲染任何容量相关的提示，也不
  // 拿它做上传前拦截。
  const [capacity, setCapacity] = useState<CapacityConfig | null>(null)

  useEffect(() => {
    let cancelled = false
    getCapacityConfig()
      .then((c) => {
        if (!cancelled) setCapacity(c)
      })
      .catch(() => {
        // 取不到就不提示、也不拦截——总比拿一份陈旧/瞎猜的数字误导用户，
        // 或者在接口故障时把上传功能锁死强。
      })
    return () => {
      cancelled = true
    }
  }, [])

  const uploading = progress !== null
  const awaitingRiskDecision = pendingFile !== null

  const graphRisk: GraphRisk =
    engine === 'graph' && pendingFile !== null && capacity !== null
      ? assessGraphRisk(pendingFile.size, capacity)
      : 'none'

  const startUpload = async (file: File, engineToUse: EngineName) => {
    setError(null)
    try {
      const { taskId } = await uploadFile(file, {
        engine: engineToUse,
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

  // 选中文件后先评估风险、再决定要不要传——不能像之前那样先
  // setPendingFile 再同步进 uploadFile：uploadFile 在第一个 await 之前
  // 就已经发出 POST /api/uploads，等 React 把警告刷到屏幕上时上传其实
  // 已经在飞了，警告成了"事后诸葛亮"，用户根本来不及改主意。
  //
  // 命中风险（graph_max_shard_bytes / merge 预算 / 分片总容量任一档）时
  // 停下来等用户从两个按钮里选一个；没有风险（或者引擎不是 graph、或者
  // capacity 还没取到）时直接上传，不给正常路径加多余的一次点击。
  const handleFileSelected = (file: File) => {
    setError(null)
    const risk =
      engine === 'graph' && capacity !== null ? assessGraphRisk(file.size, capacity) : 'none'
    if (risk !== 'none') {
      setPendingFile(file)
      return
    }
    // 复审发现：这个分支之前没清 pendingFile——选了风险文件、还没点按钮
    // 就改选另一个没风险的文件时，旧提示会原样挂在屏幕上（指向一个已经
    // 放弃的文件），awaitingRiskDecision 也会永久为 true，把
    // ConversionOptionsPanel 锁死到再也切不了引擎。
    setPendingFile(null)
    void startUpload(file, engine)
  }

  const confirmProceedWithGraph = () => {
    if (!pendingFile) return
    const file = pendingFile
    setPendingFile(null)
    void startUpload(file, 'graph')
  }

  const confirmSwitchToLibreOffice = () => {
    if (!pendingFile) return
    const file = pendingFile
    setPendingFile(null)
    setEngine('libreoffice')
    void startUpload(file, 'libreoffice')
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
        <UploadDropzone
          onFileSelected={handleFileSelected}
          maxBytes={capacity?.max_file_size ?? MAX_BYTES}
        />

        {pendingFile !== null && graphRisk !== 'none' && (
          <div
            className="card"
            style={{
              padding: 'var(--space-3)',
              borderLeft: '4px solid var(--c-notable)',
              fontSize: 13,
              lineHeight: 1.6,
            }}
          >
            <p>{GRAPH_RISK_MESSAGE[graphRisk]}</p>
            <div
              style={{
                display: 'flex',
                gap: 'var(--space-2)',
                marginTop: 'var(--space-3)',
              }}
            >
              <button type="button" className="btn btn-ghost" onClick={confirmProceedWithGraph}>
                仍然继续
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={confirmSwitchToLibreOffice}
              >
                改用 LibreOffice 并继续
              </button>
            </div>
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
          disabled={uploading || awaitingRiskDecision}
        />
      </div>

      <div className="col">
        <span className="section-title">任务</span>
        <TaskList taskIds={taskIds} />
      </div>
    </div>
  )
}
