import { useEffect, useState } from 'react'

import { useI18n } from '../../i18n'
import { deleteFont, listFonts, type FontFile, type FontList } from '../../lib/adminApi'

const EMPTY: FontList = { managed: [], mounted: [], builtin: [] }

function mib(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/**
 * 字体列表面板。
 *
 * 三组必须分开显示，不能合并成一张表：managed 是面板自己传上来的，能删；
 * mounted 是运维手工塞进 fonts-extra 的只读挂载；builtin 是镜像里带的
 * 300+ 个文件。后两组给了删除按钮只会让人点了报错——容器里它们是只读的。
 *
 * builtin 默认折叠、展开才带 include_builtin=true 重新请求：扫一遍是
 * 秒级的（每个文件要跑两次 fc-query 取 family/覆盖范围），不该一进面板
 * 就替所有人付这个代价。
 */
export function FontsPanel() {
  const { t } = useI18n()
  const [data, setData] = useState<FontList>(EMPTY)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [builtinExpanded, setBuiltinExpanded] = useState(false)
  const [builtinLoading, setBuiltinLoading] = useState(false)

  const reload = (includeBuiltin: boolean) =>
    listFonts(includeBuiltin)
      .then(setData)
      .catch((e: Error) => setError(e.message))

  useEffect(() => {
    void reload(false)
  }, [])

  const handleExpandBuiltin = async () => {
    setBuiltinLoading(true)
    setError(null)
    try {
      await reload(true)
      setBuiltinExpanded(true)
    } finally {
      setBuiltinLoading(false)
    }
  }

  const handleDelete = async (font: FontFile) => {
    if (!window.confirm(t('admin.fonts.confirmDelete', { filename: font.filename }))) return
    setBusyId(font.file_id)
    setError(null)
    try {
      await deleteFont(font.file_id)
      await reload(builtinExpanded)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusyId(null)
    }
  }

  const renderRow = (font: FontFile) => (
    <div
      key={font.file_id}
      style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}
    >
      <strong>{font.filename}</strong>
      <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
        {t('admin.fonts.families')}: {font.families.join('、')}
      </span>
      <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
        {t('admin.fonts.version')}: {font.version}
      </span>
      <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
        {t('admin.fonts.coverage', { count: font.charset_count.toLocaleString() })}
      </span>
      <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>{mib(font.size_bytes)}</span>
      <span style={{ fontSize: 12, color: 'var(--c-text-dim)' }}>
        {new Date(font.modified_at).toLocaleString()}
      </span>
      {font.deletable && (
        <button
          type="button"
          className="btn btn-ghost"
          style={{ marginLeft: 'auto' }}
          disabled={busyId === font.file_id}
          onClick={() => void handleDelete(font)}
        >
          {t('common.delete')}
        </button>
      )}
    </div>
  )

  return (
    <>
      {error && (
        <p role="alert" className="alert alert-danger">
          {error}
        </p>
      )}

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">{t('admin.fonts.managed')}</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          {data.managed.map(renderRow)}
          {data.managed.length === 0 && (
            <p style={{ color: 'var(--c-text-dim)', margin: 0 }}>{t('admin.fonts.empty')}</p>
          )}
        </div>
      </div>

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">{t('admin.fonts.mounted')}</span>
        <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
          {data.mounted.map(renderRow)}
          {data.mounted.length === 0 && (
            <p style={{ color: 'var(--c-text-dim)', margin: 0 }}>{t('admin.fonts.mountedEmpty')}</p>
          )}
        </div>
      </div>

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <span className="section-title">{t('admin.fonts.builtin')}</span>
        {!builtinExpanded ? (
          <button
            type="button"
            className="btn btn-ghost"
            disabled={builtinLoading}
            onClick={() => void handleExpandBuiltin()}
          >
            {builtinLoading ? t('common.loading') : t('admin.fonts.loadBuiltin')}
          </button>
        ) : (
          <div style={{ display: 'grid', gap: 'var(--space-2)' }}>
            {data.builtin.map(renderRow)}
            {data.builtin.length === 0 && (
              <p style={{ color: 'var(--c-text-dim)', margin: 0 }}>{t('admin.fonts.empty')}</p>
            )}
          </div>
        )}
      </div>
    </>
  )
}
