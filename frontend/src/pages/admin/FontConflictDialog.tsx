import { useEffect, useRef, useState } from 'react'

import { useI18n } from '../../i18n'
import type { FontFile, FontPreflight } from '../../lib/adminApi'

function mib(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// 候选比上传的多出这个比例以上才报警。版本号看不出子集化，字数能——
// 但两个字体统计口径本来就有些误差，5% 以内的抖动不该被当成精简版。
const SUBSET_WARNING_RATIO = 0.05

/**
 * 上传字体撞名时的冲突确认弹窗。
 *
 * 不做「这是更新还是新字体」的自动判断——判断不了：字体子集化很常见
 * （family 和版本号可能都一样，只是字形少一半），厂商版本号也不规范。
 * 任何自动判断都会在某些字体上出错，代价是管理员莫名其妙丢字体。
 *
 * 所以这里只摆事实，把决定权交给人：每个候选一个独立勾选框（一次可能
 * 要替换多个——旧字体拆成三个文件、新的合并成一个，是真实场景，单
 * 按钮表达不了），以及一条基于字数对比的警告，而不是猜测。
 */
export function FontConflictDialog({
  preflight,
  onResolve,
  onCancel,
}: {
  preflight: FontPreflight
  onResolve: (replace: string[]) => void
  onCancel: () => void
}) {
  const { t } = useI18n()
  const { incoming, candidates } = preflight
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)

  // Esc 与点遮罩都等同于「取消上传」，不是静默关闭：preflight 阶段已经
  // 在后端生成了暂存文件，半途关掉弹窗而不给后端任何信号，那个暂存
  // 文件会一直挂到 TTL 过期。
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onCancel])

  // 打开时把焦点移进模态，卸载后还给触发上传的按钮——键盘用户不该被
  // 留在一个已经消失的元素上。与 UserMenu 的登录弹窗同一套写法。
  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement | null
    dialogRef.current?.focus()
    return () => {
      previousFocusRef.current?.focus?.()
    }
  }, [])

  const toggle = (fileId: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(fileId)) next.delete(fileId)
      else next.add(fileId)
      return next
    })
  }

  const renderCandidate = (font: FontFile) => {
    const disabled = !font.deletable
    // 候选（已有字体）比上传的多出的字数：这也正是勾选替换后会丢掉的字数——
    // 候选字数明显多于上传的，才提示，这是精简版（子集化）最直接的信号。
    const wouldLose = font.charset_count - incoming.charset_count
    const showWarning =
      incoming.charset_count > 0 && wouldLose > incoming.charset_count * SUBSET_WARNING_RATIO

    return (
      <div
        key={font.file_id}
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 'var(--space-1)',
          padding: 'var(--space-2) 0',
          borderTop: '1px solid var(--c-border)',
        }}
      >
        <label
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 'var(--space-2)',
            flexWrap: 'wrap',
          }}
        >
          <input
            type="checkbox"
            checked={selected.has(font.file_id)}
            disabled={disabled}
            onChange={() => toggle(font.file_id)}
          />
          <strong>{font.filename}</strong>
          <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
            {t('admin.fonts.families')}: {font.families.join(t('admin.fonts.familySeparator'))}
          </span>
          <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
            {t('admin.fonts.version')}: {font.version}
          </span>
          <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
            {t('admin.fonts.coverage', { count: font.charset_count.toLocaleString() })}
          </span>
          <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
            {new Date(font.modified_at).toLocaleString()}
          </span>
          {disabled && (
            <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
              (
              {font.source === 'builtin'
                ? t('admin.fonts.notReplaceable.builtin')
                : t('admin.fonts.notReplaceable.mounted')}
              )
            </span>
          )}
        </label>
        {showWarning && (
          <p role="alert" className="alert alert-danger" style={{ margin: 0, fontSize: 12 }}>
            {t('admin.fonts.wouldLoseChars', { count: wouldLose.toLocaleString() })}
          </p>
        )}
      </div>
    )
  }

  return (
    <div
      className="glass-overlay"
      onMouseDown={(e) => {
        // 只有点在遮罩本身（而不是冒泡上来的模态内部）才算取消
        if (e.target === e.currentTarget) onCancel()
      }}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="glass-strong glass-modal"
        role="dialog"
        aria-modal="true"
        aria-label={t('admin.fonts.conflictTitle')}
        style={{ width: 'min(640px, 100%)' }}
      >
        <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
          <span className="section-title">{t('admin.fonts.conflictTitle')}</span>

          <div
            className="card glass"
            style={{ padding: 'var(--space-3)', display: 'grid', gap: 'var(--space-1)' }}
          >
            <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
              {t('admin.fonts.incoming')}
            </span>
            <strong>{incoming.filename}</strong>
            <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
              {t('admin.fonts.families')}: {incoming.families.join(t('admin.fonts.familySeparator'))}
            </span>
            <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
              {t('admin.fonts.version')}: {incoming.version} ·{' '}
              {t('admin.fonts.coverage', { count: incoming.charset_count.toLocaleString() })} ·{' '}
              {mib(incoming.size_bytes)}
            </span>
          </div>

          <div style={{ display: 'grid' }}>{candidates.map(renderCandidate)}</div>

          <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
            <button
              type="button"
              className="btn btn-primary"
              disabled={selected.size === 0}
              onClick={() => onResolve(Array.from(selected))}
            >
              {t('admin.fonts.replaceSelected')}
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => onResolve([])}>
              {t('admin.fonts.isNewFont')}
            </button>
            <button type="button" className="btn btn-ghost" onClick={onCancel}>
              {t('admin.fonts.cancelUpload')}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
