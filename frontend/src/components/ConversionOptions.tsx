import type { ConversionOptions, EngineName } from '../lib/api'

interface Props {
  engine: EngineName
  onEngineChange: (engine: EngineName) => void
  options: ConversionOptions
  onOptionsChange: (options: ConversionOptions) => void
  disabled?: boolean
}

const ENGINES: {
  value: EngineName
  title: string
  hint: string
}[] = [
  {
    value: 'libreoffice',
    title: 'LibreOffice',
    hint: '无页数与超时限制，长 deck 的主力',
  },
  {
    value: 'graph',
    title: 'Microsoft Graph',
    hint: '高保真，但受 100 页 / 45 秒限制 · 三期实现',
  },
]

const POST_OPTIONS: {
  key: keyof ConversionOptions
  label: string
  hint: string
}[] = [
  {
    key: 'expand_animations',
    label: '动画分步展开',
    hint: '按 p:timing 把逐步出现的内容拆成多页，避免所有元素叠在一页上',
  },
  {
    key: 'pdf_outline',
    label: 'PDF 书签大纲',
    hint: '用每页标题生成书签，方便在 GoodNotes 里跳转',
  },
  {
    key: 'remap_margins',
    label: '页边距重映射',
    hint: '给 iPad 竖屏批注留出侧边空白',
  },
]

export function ConversionOptionsPanel({
  engine,
  onEngineChange,
  options,
  onOptionsChange,
  disabled = false,
}: Props) {
  return (
    <div className="card" style={{ padding: 'var(--space-4)' }}>
      <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
        <span className="section-title">转换引擎</span>
        <div className="segmented">
          {ENGINES.map((e) => (
            <button
              key={e.value}
              type="button"
              className="segment"
              aria-pressed={engine === e.value}
              disabled={disabled}
              onClick={() => onEngineChange(e.value)}
            >
              <span className="segment-title">{e.title}</span>
              <span className="segment-hint">{e.hint}</span>
            </button>
          ))}
        </div>
      </div>

      <div
        style={{
          display: 'grid',
          gap: 'var(--space-1)',
          marginTop: 'var(--space-5)',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 'var(--space-2)',
            marginBottom: 'var(--space-1)',
          }}
        >
          <span className="section-title">后处理</span>
          <span className="badge badge-warn">后端实现中</span>
        </div>

        {POST_OPTIONS.map((o) => (
          <label key={o.key} className="check-row">
            <input
              type="checkbox"
              checked={options[o.key]}
              disabled={disabled}
              onChange={(e) =>
                onOptionsChange({ ...options, [o.key]: e.target.checked })
              }
            />
            <span>
              <span className="check-label">{o.label}</span>
              <span className="check-hint" style={{ display: 'block' }}>
                {o.hint}
              </span>
            </span>
          </label>
        ))}
      </div>
    </div>
  )
}
