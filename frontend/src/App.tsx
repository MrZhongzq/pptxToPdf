import { useEffect, useState } from 'react'
import { ConversionOptionsPanel } from './components/ConversionOptions'
import { ReadyCard } from './components/ReadyCard'
import { TaskList } from './components/TaskList'
import { UploadDropzone } from './components/UploadDropzone'
import { UploadProgress } from './components/UploadProgress'
import {
  ApiError,
  DEFAULT_OPTIONS,
  getCapacityConfig,
  startTask,
  type CapacityConfig,
  type ConversionOptions,
  type EngineName,
} from './lib/api'
import { assessGraphRisk, GRAPH_RISK_MESSAGE, type GraphRisk } from './lib/graphCapacity'
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

export default function App() {
  // 只有两个页面，一个 pathname 判断足够——不引入 react-router 这个依赖。
  // 这层判断必须放在一个零 hooks 的外层组件里：两个分支各自渲染独立的
  // 组件实例（<AdminPage /> 或 <UploadPage />），而不是在同一个组件实例
  // 内部、hooks 调用之前提前 return。后者在当前"纯整页刷新、无客户端
  // 路由"的前提下不会真的崩，但一旦以后有任何方式让同一个已挂载的 App
  // 实例在 pathname 变化后重渲染（哪怕只是给 /admin 加一个不用 <a href>
  // 的"返回上传页"按钮），就会从"提前 return、零 hooks"切到"完整调用
  // 全部 hooks"，触发 React 的 rules-of-hooks 校验失败。外层零 hooks 天然
  // 不可能违反这条规则，内层组件要么完整挂载要么完全不挂载。
  if (typeof window !== 'undefined' && window.location.pathname.startsWith('/admin')) {
    return <AdminPage />
  }
  return <UploadPage />
}

/** complete 之后落在 ready 状态的任务——只留展示 ReadyCard 需要的三样，
 *  不取整份 TaskDto：文件名/体积上传当下就知道，不用多打一次
 *  GET /api/tasks/{id} 换一份此刻还没有其它字段可填的快照。 */
interface ReadyTask {
  taskId: string
  filename: string
  sizeBytes: number
}

function UploadPage() {
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
  // 五期两段式上传：传完先停在这里，等用户点「开始转换」，不直接入队。
  const [readyTask, setReadyTask] = useState<ReadyTask | null>(null)
  // 只用来给风险确认横幅上的两个按钮做防抖——ReadyCard 自己的按钮已经
  // 用组件内部的 starting 挡过一次快速点击，但确认横幅是 App 直接渲染的，
  // 不经过 ReadyCard，没有那层保护，得单独补上。
  const [startingReadyTask, setStartingReadyTask] = useState(false)

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

  // 上传前的风险预判决策点在"选文件时"（engine 那时已经定了）；这一份是
  // 它的镜像，决策点后移到"点开始转换时"——引擎现在可以在 ReadyCard 上
  // 换，只有真要发 start 请求的这一刻才需要重新评估。两套判定各自独立，
  // 互不影响：改这一份不会碰到上面 pendingFile 那份的任何一行。
  const readyGraphRisk: GraphRisk =
    engine === 'graph' && readyTask !== null && capacity !== null
      ? assessGraphRisk(readyTask.sizeBytes, capacity)
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
      // 不直接入队——用户原话「有时候手没那么快，想先上传再选转换引擎和
      // 选项」。落在 ready 状态，等 ReadyCard 上点「开始转换」才真正入队。
      setReadyTask({ taskId, filename: file.name, sizeBytes: file.size })
      setProgress(null)
    } catch (err) {
      setProgress(null)
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  // 真正把 ready 任务送入队列。engine/options 单独传参而不是闭包读
  // App 的 engine/options state——"仍然继续"/"改用 LibreOffice"这两个
  // confirm 分支里，setEngine 还没来得及触发重渲染，闭包里的 engine 仍是
  // 旧值，必须显式传当前要用的那个，跟四期 startUpload(file, engineToUse)
  // 同一个理由。
  const handleStart = async (engineToUse: EngineName, optionsToUse: ConversionOptions) => {
    if (!readyTask) return
    const taskId = readyTask.taskId
    setStartingReadyTask(true)
    setError(null)
    try {
      await startTask(taskId, engineToUse, optionsToUse)
      setTaskIds((prev) => [taskId, ...prev])
      setReadyTask(null)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'TASK_ALREADY_STARTED') {
        // 任务已经真的被启动过一次（比如另一个标签页抢先点了「开始转换」）
        // ——它在正常转换，接上轮询才是对用户有用的恢复，不是当错误处理。
        setTaskIds((prev) => [taskId, ...prev])
        setReadyTask(null)
      } else if (err instanceof ApiError && err.code === 'READY_EXPIRED') {
        // ready 任务有 1 小时 TTL，原文件已经被回收——ReadyCard 停在原地
        // 再点也没用，退回可以重新上传的状态。message 用后端已经写好的
        // 原话（含具体 TTL 小时数），这里不重复拼一遍。
        setReadyTask(null)
        setError(err.message)
      } else {
        // 其它错误（网络故障、后端 500 等）：ReadyCard 原样留着，用户能
        // 直接重试，不该因为一次瞬时错误就强迫已经传好的文件作废重传。
        setError(
          err instanceof ApiError
            ? `${err.code}：${err.message}`
            : err instanceof Error
              ? err.message
              : '启动转换失败',
        )
      }
    } finally {
      setStartingReadyTask(false)
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

  const confirmReadyProceedWithGraph = () => {
    void handleStart('graph', options)
  }

  const confirmReadySwitchToLibreOffice = () => {
    setEngine('libreoffice')
    void handleStart('libreoffice', options)
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

        {readyTask ? (
          <>
            <ReadyCard
              filename={readyTask.filename}
              sizeBytes={readyTask.sizeBytes}
              engine={engine}
              onEngineChange={setEngine}
              options={options}
              onOptionsChange={setOptions}
              onStart={handleStart}
              disabled={readyGraphRisk !== 'none'}
            />

            {readyGraphRisk !== 'none' && (
              <div
                className="card"
                style={{
                  padding: 'var(--space-3)',
                  borderLeft: '4px solid var(--c-notable)',
                  fontSize: 13,
                  lineHeight: 1.6,
                }}
              >
                <p>{GRAPH_RISK_MESSAGE[readyGraphRisk]}</p>
                <div
                  style={{
                    display: 'flex',
                    gap: 'var(--space-2)',
                    marginTop: 'var(--space-3)',
                  }}
                >
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={startingReadyTask}
                    onClick={confirmReadyProceedWithGraph}
                  >
                    仍然继续
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={startingReadyTask}
                    onClick={confirmReadySwitchToLibreOffice}
                  >
                    改用 LibreOffice 并继续
                  </button>
                </div>
              </div>
            )}
          </>
        ) : (
          <ConversionOptionsPanel
            engine={engine}
            onEngineChange={setEngine}
            options={options}
            onOptionsChange={setOptions}
            disabled={uploading || awaitingRiskDecision}
          />
        )}
      </div>

      <div className="col">
        <span className="section-title">任务</span>
        <TaskList taskIds={taskIds} />
      </div>
    </div>
  )
}
