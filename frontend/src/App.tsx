import { useEffect, useRef, useState } from 'react'
import { ConversionOptionsPanel } from './components/ConversionOptions'
import { useI18n } from './i18n'
import { UserMenu } from './components/UserMenu'
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
  type UserDto,
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
  const { t } = useI18n()
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
  const [currentUser, setCurrentUser] = useState<UserDto | null>(null)
  const [readyTask, setReadyTask] = useState<ReadyTask | null>(null)
  // 复审 Important：UploadDropzone 在有 readyTask 时依旧照常可点可拖，
  // 用户传完 A 去泡咖啡、回来传 B 是完全正常的操作路径，不是多标签页
  // 才会撞见的边角情形。直接用 B 覆盖 A 会让 A 的卡片无声消失、任务
  // 列表里也找不到（A 从没进过 taskIds），1 小时后被 TTL 静默回收——
  // 跟"选了 Graph 却背着用户转 LibreOffice"是同一类"操作结果没告知就
  // 被清除"，一样属于"绝不静默"要挡住的范围。这里存一下待确认的新文件，
  // 等用户明确选"继续上传"才真的丢弃旧的 ready 任务。
  const [pendingReplacementFile, setPendingReplacementFile] = useState<File | null>(null)
  // 复审 Important（第二轮）：handleStart 里 setReadyTask(null) 发生在
  // await startTask(...) 之后，这段异步窗口里 UploadDropzone 从未被禁用，
  // 用户可以在窗口期间选中文件、让 pendingReplacementFile 从 null 变成
  // 非空。handleStart 的 async 函数体是按"调用那一刻"的渲染闭包在跑的，
  // await 恢复后再读闭包里的 pendingReplacementFile 拿到的还是调用时刻
  // 的旧值（这一整个函数体不会因为后续 setState 而重新求值）——必须用
  // ref 才能在 await 之后读到真正最新的值。
  const pendingReplacementFileRef = useRef<File | null>(null)
  useEffect(() => {
    pendingReplacementFileRef.current = pendingReplacementFile
  }, [pendingReplacementFile])
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

  const startUpload = async (
    file: File,
    engineToUse: EngineName,
    optionsToUse: ConversionOptions,
  ) => {
    setError(null)
    try {
      const { taskId } = await uploadFile(file, {
        engine: engineToUse,
        options: optionsToUse,
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

  // 选中文件后先评估风险、再决定要不要传——不能像之前那样先
  // setPendingFile 再同步进 uploadFile：uploadFile 在第一个 await 之前
  // 就已经发出 POST /api/uploads，等 React 把警告刷到屏幕上时上传其实
  // 已经在飞了，警告成了"事后诸葛亮"，用户根本来不及改主意。
  //
  // 命中风险（graph_max_shard_bytes / merge 预算 / 分片总容量任一档）时
  // 停下来等用户从两个按钮里选一个；没有风险（或者引擎不是 graph、或者
  // capacity 还没取到）时直接上传，不给正常路径加多余的一次点击。
  // Graph 风险判定 + 上传，两个入口共用（正常选文件、确认覆盖旧 ready
  // 任务之后），保证两条路径的判定逻辑不会各写一份、慢慢跑偏。
  //
  // 复审 Important-1：engineToUse/optionsToUse 必须是显式参数，不能读
  // App 的 engine/options state——这个函数可能在 handleStart 的 await 之后
  // 被 clearReadyTaskAndFulfillPendingReplacement 调用，那时"这次调用"的
  // 闭包仍是 handleStart 被点击那一刻的旧渲染，若改用 setEngine 之后立刻
  // 调用（如"改用 LibreOffice 并继续"：先 setEngine('libreoffice') 再
  // handleStart(...)），闭包里的 engine 读到的还是切换前的旧值——
  // startUpload(file, engineToUse) 早已是这个约定，这里对齐同一个规则。
  const proceedWithFileSelection = (
    file: File,
    engineToUse: EngineName,
    optionsToUse: ConversionOptions,
  ) => {
    const risk =
      engineToUse === 'graph' && capacity !== null ? assessGraphRisk(file.size, capacity) : 'none'
    if (risk !== 'none') {
      setPendingFile(file)
      return
    }
    // 复审发现：这个分支之前没清 pendingFile——选了风险文件、还没点按钮
    // 就改选另一个没风险的文件时，旧提示会原样挂在屏幕上（指向一个已经
    // 放弃的文件），awaitingRiskDecision 也会永久为 true，把
    // ConversionOptionsPanel 锁死到再也切不了引擎。
    setPendingFile(null)
    void startUpload(file, engineToUse, optionsToUse)
  }

  // 复审 Important（第二轮）：readyTask 的清空点有三处（start 成功、
  // TASK_ALREADY_STARTED 接入轮询、READY_EXPIRED 过期），全在 handleStart
  // 的 await 之后。这三种情况共同点是——"是否要放弃当前 ready 任务"这个
  // 问题都已经自行解决（成功启动了/已经在别处跑了/已经过期了，都不再
  // 有"被放弃"的风险）。如果这时候 pendingReplacementFileRef 里还压着一个
  // 用户在等待期间选中的文件，那个确认横幅问的问题已经不存在了，但用户
  // 选那个文件的真实意图——"传它"——仍然存在，不能因为问题恰好消失就把
  // 意图也一起吞掉。这里统一清空 readyTask 的同时，把压着的文件"兑现"：
  // 直接送进 proceedWithFileSelection，跟用户自己点"继续上传"是同一条
  // 路径，不另写一套。
  //
  // 只用 ref 不用闭包里的 pendingReplacementFile：handleStart 是在按钮
  // 点击那一刻的渲染闭包里执行的，await 恢复后再读闭包变量拿到的是调用
  // 时刻的旧值，不会跟着后续的 setPendingReplacementFile 变化——ref 才是
  // 调用时刻之后仍然实时的那份。
  //
  // 复审 Important-1：engineToUse/optionsToUse 必须由调用方（handleStart）
  // 显式传入，不能让这里或 proceedWithFileSelection 自己去读 App 的
  // engine/options state——这个函数本身也是 handleStart 调用那一刻那次
  // render 的闭包实例，"改用 LibreOffice 并继续"先 setEngine('libreoffice')
  // 再调 handleStart(...)，此时闭包里的 engine 仍是切换前的 'graph'。
  // 之前只把这条规则用在 startTask(taskId, engineToUse, optionsToUse) 上，
  // 却在紧接着的兑现逻辑里读了闭包——同一个函数违反了它自己上面几行注释
  // 写的规则。PROBE-8 实测：这会让"兑现"变成另一个静默黑洞（B 被错误地
  // 判成 graph 风险、卡进 pendingFile 永不上传），还会把
  // ConversionOptionsPanel 永久锁死（awaitingRiskDecision 恒真）。
  const clearReadyTaskAndFulfillPendingReplacement = (
    engineToUse: EngineName,
    optionsToUse: ConversionOptions,
  ) => {
    setReadyTask(null)
    const pending = pendingReplacementFileRef.current
    if (pending) {
      setPendingReplacementFile(null)
      proceedWithFileSelection(pending, engineToUse, optionsToUse)
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
      clearReadyTaskAndFulfillPendingReplacement(engineToUse, optionsToUse)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'TASK_ALREADY_STARTED') {
        // 任务已经真的被启动过一次（比如另一个标签页抢先点了「开始转换」）
        // ——它在正常转换，接上轮询才是对用户有用的恢复，不是当错误处理。
        setTaskIds((prev) => [taskId, ...prev])
        clearReadyTaskAndFulfillPendingReplacement(engineToUse, optionsToUse)
      } else if (err instanceof ApiError && err.code === 'READY_EXPIRED') {
        // ready 任务有 1 小时 TTL，原文件已经被回收——ReadyCard 停在原地
        // 再点也没用，退回可以重新上传的状态。message 用后端已经写好的
        // 原话（含具体 TTL 小时数），这里不重复拼一遍。
        clearReadyTaskAndFulfillPendingReplacement(engineToUse, optionsToUse)
        setError(err.message)
      } else {
        // 终审 M-4：这句"重试"对多数瞬时错误（网络故障等）成立——原文件
        // 还在，ReadyCard 原样留着确实能直接重试。但对 503
        // ENGINE_UNAVAILABLE 不成立：start_task 那条路径已经
        // drop_original 并把任务标 failed（app/api/tasks.py），原文件
        // 已经没了。用户看着 ReadyCard 再点一次「开始转换」，实际拿到的
        // 是 409 TASK_ALREADY_STARTED——但任务真实状态是 failed，不是
        // "已经在正常跑"，上面那个分支"接上轮询"的假设在这条路径下并
        // 不成立。
        //
        // 这条分支不清 readyTask，pendingReplacementFile 也就不去动它——
        // 它对应的是"用户还没决定要不要放弃当前 ready 任务"，跟上面三条
        // "问题已经自行解决"的情形不是一回事，不能套同一个兑现逻辑。
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

  const handleFileSelected = (file: File) => {
    setError(null)
    if (readyTask !== null) {
      // 有一个待开始的任务——先问，问之前不发任何上传请求。跟四期的
      // 容量确认同一条原则：不可逆的丢弃发生前必须停下来问。
      setPendingReplacementFile(file)
      return
    }
    proceedWithFileSelection(file, engine, options)
  }

  const confirmReplaceReadyTask = () => {
    if (!pendingReplacementFile) return
    const file = pendingReplacementFile
    setPendingReplacementFile(null)
    setError(null)
    setReadyTask(null)
    proceedWithFileSelection(file, engine, options)
  }

  const cancelReplaceReadyTask = () => {
    setPendingReplacementFile(null)
  }

  const confirmProceedWithGraph = () => {
    if (!pendingFile) return
    const file = pendingFile
    setPendingFile(null)
    void startUpload(file, 'graph', options)
  }

  const confirmSwitchToLibreOffice = () => {
    if (!pendingFile) return
    const file = pendingFile
    setPendingFile(null)
    setEngine('libreoffice')
    void startUpload(file, 'libreoffice', options)
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
      <header className="page-head" style={{ position: 'relative' }}>
        <div>
          <h1 className="page-title">{t('app.title')}</h1>
          <span className="page-sub">{t('app.subtitle')}</span>
        </div>
        <div style={{ position: 'absolute', top: 0, right: 0 }}>
          <UserMenu onUserChange={setCurrentUser} />
        </div>
      </header>

      <div className="col">
        <UploadDropzone
          onFileSelected={handleFileSelected}
          maxBytes={capacity?.max_file_size ?? MAX_BYTES}
        />

        {pendingReplacementFile !== null && readyTask !== null && (
          <div
            className="card glass"
            style={{
              padding: 'var(--space-3)',
              borderLeft: '4px solid var(--c-notable)',
              fontSize: 13,
              lineHeight: 1.6,
            }}
          >
            <p>
              当前有一个待开始的任务《{readyTask.filename}》，继续上传会放弃它。
            </p>
            <div
              style={{
                display: 'flex',
                gap: 'var(--space-2)',
                marginTop: 'var(--space-3)',
              }}
            >
              <button type="button" className="btn btn-ghost" onClick={cancelReplaceReadyTask}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={confirmReplaceReadyTask}
              >
                继续上传
              </button>
            </div>
          </div>
        )}

        {pendingFile !== null && graphRisk !== 'none' && (
          <div
            className="card glass"
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
              // 复审 Minor-3：startingReadyTask 必须也在这里——「改用
              // LibreOffice 并继续」先 setEngine('libreoffice') 再
              // handleStart(...)，下一次渲染 readyGraphRisk 会立刻因为
              // engine 已经不是 'graph' 而变回 'none'，风险横幅连同它的
              // disabled 一起消失；但此时 handleStart 的 startTask 请求
              // 仍在飞（这次 handleStart 不经过 ReadyCard 自己的 onStart，
              // ReadyCard 内部的 starting 状态从未被置位）。不加这一条，
              // ReadyCard 的「开始转换」在飞行期间完全可点，实测会让同一个
              // taskId 被 start 两次，第二次拿 409 又被当成「已经在跑」加
              // 进任务列表，同一个 taskId 进两次、TaskList 重复 key。
              // 刻意**不含** readyGraphRisk：风险横幅现在就地占据「开始转换」
              // 的位置（七期），用户已经没有「绕过横幅直接开始」的路径，六期
              // 那条整卡禁用因此失去了理由，只剩下把引擎与后处理选项一起锁死
              // 的副作用。而横幅上就摆着「改用 LibreOffice 并继续」——允许改
              // 引擎却不让点引擎按钮，前后矛盾。放开之后，用户点 LibreOffice
              // 会让 readyGraphRisk 变回 'none'，横幅自动消失、按钮回来。
              disabled={pendingReplacementFile !== null || startingReadyTask}
              // 风险确认就地占据「开始转换」的位置，见 ReadyCard 里的注释。
              riskMessage={
                readyGraphRisk !== 'none' ? GRAPH_RISK_MESSAGE[readyGraphRisk] : null
              }
              // 终审 I-5：必须跟 ReadyCard 自己的 disabled 同一个条件——覆盖
              // 确认横幅同屏时，这两个按钮此前只被 startingReadyTask 挡，没被
              // pendingReplacementFile 挡。用户能点"仍然继续"启动 A，
              // setReadyTask(null) 让覆盖横幅因 readyTask===null 而消失，但
              // pendingReplacementFile 里的 B 从未上传也从未告知，构成静默丢弃；
              // 那份残留还会在后续任务落 ready 时冒出一条张冠李戴的横幅。
              riskActionsDisabled={startingReadyTask || pendingReplacementFile !== null}
              onProceedWithGraph={confirmReadyProceedWithGraph}
              onSwitchToLibreOffice={confirmReadySwitchToLibreOffice}
            />

          </>
        ) : (
          <ConversionOptionsPanel
            engine={engine}
            onEngineChange={setEngine}
            options={options}
            onOptionsChange={setOptions}
            disabled={uploading || awaitingRiskDecision}
            loggedIn={currentUser !== null}
          />
        )}
      </div>

      <div className="col">
        <span className="section-title">{t('task.section')}</span>
        <TaskList taskIds={taskIds} />
      </div>
    </div>
  )
}
