import { useI18n } from '../i18n'
import type { ConversionOptions, EngineName } from '../lib/api'

interface Props {
  engine: EngineName
  onEngineChange: (engine: EngineName) => void
  options: ConversionOptions
  onOptionsChange: (options: ConversionOptions) => void
  disabled?: boolean
  /** 未登录时 Graph 通道不可选。前端置灰只是体验，后端 start_task 才是边界。 */
  loggedIn?: boolean
}

const ENGINES: {
  value: EngineName
  titleKey: string
  hintKey: string
}[] = [
  {
    value: 'libreoffice',
    titleKey: 'engine.libreoffice.title',
    hintKey: 'engine.libreoffice.hint',
  },
  {
    value: 'graph',
    titleKey: 'engine.graph.title',
    hintKey: 'engine.graph.hint',
  },
]

const POST_OPTIONS: {
  key: keyof ConversionOptions
  labelKey: string
  hintKey: string
}[] = [
  {
    key: 'expand_animations',
    labelKey: 'postprocess.animations.label',
    hintKey: 'postprocess.animations.hint',
  },
  {
    key: 'pdf_outline',
    labelKey: 'postprocess.outline.label',
    hintKey: 'postprocess.outline.hint',
  },
  {
    key: 'remap_margins',
    labelKey: 'postprocess.margins.label',
    hintKey: 'postprocess.margins.hint',
  },
]

export function ConversionOptionsPanel({
  engine,
  onEngineChange,
  options,
  onOptionsChange,
  disabled = false,
  loggedIn = true,
}: Props) {
  const { t } = useI18n()
  return (
    <div className="card glass" style={{ padding: 'var(--space-4)' }}>
      <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
        <span className="section-title">{t('engine.section')}</span>
        <div className="segmented">
          {ENGINES.map((e) => {
            const needsLogin = e.value === 'graph' && !loggedIn
            return (
              <button
                key={e.value}
                type="button"
                className={needsLogin ? 'segment segment-locked' : 'segment'}
                aria-pressed={engine === e.value}
                disabled={disabled || needsLogin}
                onClick={() => onEngineChange(e.value)}
              >
                <span className="segment-title">
                  {t(e.titleKey)}
                  {/* 直接写出来而不是只靠 title 属性——title 在触屏上根本
                      出不来，用户只会看到一个点不动的按钮 */}
                  {needsLogin && <span className="segment-lock">{t('engine.needsLogin')}</span>}
                </span>
                <span className="segment-hint">{t(e.hintKey)}</span>
              </button>
            )
          })}
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
          <span className="section-title">{t('postprocess.section')}</span>
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
              <span className="check-label">{t(o.labelKey)}</span>
              <span className="check-hint" style={{ display: 'block' }}>
                {t(o.hintKey)}
              </span>
            </span>
          </label>
        ))}
      </div>
    </div>
  )
}
