import { useState } from 'react'
import type { ConversionOptions, EngineName } from '../lib/api'
import { formatBytes } from '../lib/chunking'
import { useI18n } from '../i18n'
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
  /** 命中 Graph 容量风险时的提示；为 null 表示无风险，正常显示开始按钮。 */
  riskMessage?: string | null
  riskActionsDisabled?: boolean
  onProceedWithGraph?: () => void
  onSwitchToLibreOffice?: () => void
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
  riskMessage = null,
  riskActionsDisabled = false,
  onProceedWithGraph,
  onSwitchToLibreOffice,
}: Props) {
  const { t } = useI18n()
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
    <div className="card glass" style={{ padding: 'var(--space-4)' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 'var(--space-3)',
        }}
      >
        <strong style={{ wordBreak: 'break-word', lineHeight: 1.4 }}>{filename}</strong>
        <span className="badge badge-neutral">{t('ready.badge')}</span>
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

      {/*
        容量风险的确认就地占据「开始转换」的位置，而不是另起一块浮在卡片
        外面。它拦的就是这个按钮所代表的动作——提示与动作分处两地时，用户
        得在两个地方找按钮，这正是用户提出要统一的那处不一致。
      */}
      {riskMessage ? (
        <div
          className="glass"
          style={{
            marginTop: 'var(--space-4)',
            padding: 'var(--space-3)',
            borderLeft: '4px solid var(--c-notable)',
            fontSize: 13,
            lineHeight: 1.6,
          }}
        >
          <p>{riskMessage}</p>
          <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-3)' }}>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={riskActionsDisabled}
              onClick={onProceedWithGraph}
            >
              {t('risk.proceedAnyway')}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={riskActionsDisabled}
              onClick={onSwitchToLibreOffice}
            >
              {t('risk.switchToLibreOffice')}
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => void handleStart()}
          disabled={disabled || starting}
          style={{ marginTop: 'var(--space-4)' }}
        >
          {starting ? t('ready.starting') : t('ready.start')}
        </button>
      )}
    </div>
  )
}
