import { useState } from 'react'
import type { ConversionOptions, EngineName } from '../lib/api'
import { formatBytes } from '../lib/chunking'
import { ConversionOptionsPanel } from './ConversionOptions'

interface Props {
  filename: string
  sizeBytes: number
  engine: EngineName
  onEngineChange: (engine: EngineName) => void
  options: ConversionOptions
  onOptionsChange: (options: ConversionOptions) => void
  /** 由 App 提供，闭包里已经绑好了 taskId；这里只负责传当前选的引擎/选项。
   *  不向调用方抛错——容量风险确认、409/410 的分支处理都是 App 层的状态
   *  转换（要不要退回可重新上传的界面），ReadyCard 自己不用懂这些语义。 */
  onStart: (engine: EngineName, options: ConversionOptions) => Promise<void>
  /** 容量预判待确认时由 App 传入，锁住引擎/选项与开始按钮，语义与
   *  ConversionOptionsPanel 自己的 disabled 一致。 */
  disabled?: boolean
  /** 未登录时 Graph 不可选，透传给 ConversionOptionsPanel。 */
  loggedIn?: boolean
}

export function ReadyCard({
  filename,
  sizeBytes,
  engine,
  onEngineChange,
  options,
  onOptionsChange,
  onStart,
  disabled = false,
  loggedIn = true,
}: Props) {
  // 用户原话「有时候手没那么快」——开始转换前留出改引擎/选项的窗口，这个
  // 组件存在的全部理由。starting 只锁按钮本身，不锁上面的选择面板之外的
  // 东西，好让「请求进行中禁用」这条对四期自检按钮同理的规则单独可测。
  const [starting, setStarting] = useState(false)

  async function handleStart() {
    setStarting(true)
    try {
      await onStart(engine, options)
    } finally {
      setStarting(false)
    }
  }

  return (
    <div className="card" style={{ padding: 'var(--space-4)' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 'var(--space-3)',
        }}
      >
        <strong style={{ wordBreak: 'break-word', lineHeight: 1.4 }}>{filename}</strong>
        <span className="badge badge-neutral">待开始</span>
      </div>

      <p
        style={{
          fontSize: 13,
          color: 'var(--c-text-dim)',
          marginTop: 'var(--space-2)',
        }}
      >
        {formatBytes(sizeBytes)}
      </p>

      <div style={{ marginTop: 'var(--space-4)' }}>
        <ConversionOptionsPanel
          loggedIn={loggedIn}
          engine={engine}
          onEngineChange={onEngineChange}
          options={options}
          onOptionsChange={onOptionsChange}
          disabled={disabled || starting}
        />
      </div>

      <button
        type="button"
        className="btn btn-primary"
        onClick={() => void handleStart()}
        disabled={disabled || starting}
        style={{ marginTop: 'var(--space-4)' }}
      >
        {starting ? '启动中…' : '开始转换'}
      </button>
    </div>
  )
}
