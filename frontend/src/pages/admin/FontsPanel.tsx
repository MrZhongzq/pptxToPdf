import { useEffect, useRef, useState, type ChangeEvent } from 'react'

import { useI18n } from '../../i18n'
import {
  commitFont,
  deleteFont,
  listFonts,
  preflightFont,
  type FontFile,
  type FontList,
  type FontPreflight,
} from '../../lib/adminApi'
import { FontConflictDialog } from './FontConflictDialog'

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
  const [uploading, setUploading] = useState(false)
  const [duplicateNotice, setDuplicateNotice] = useState<string | null>(null)
  const [conflict, setConflict] = useState<FontPreflight | null>(null)
  const generation = useRef(0)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  // 冲突弹窗的决定权在用户手上，而这里是个 for 循环——用一个 ref 存
  // 当前那次 preflight 对应的 resolve，弹窗点了按钮就唤醒它，循环才
  // 能继续处理下一个文件。resolve(null) 表示这一个文件被取消上传。
  const conflictResolverRef = useRef<((replace: string[] | null) => void) | null>(null)

  const reload = (includeBuiltin: boolean) => {
    // 每次请求领一个号，回来时只有最新那一代才允许写状态。
    // 初始加载与「展开内置」可能并发在途：初始那个不含 builtin，
    // 若它后落地就会把已展开的列表盖成空的，界面显示「展开了但没有
    // 内置字体」，管理员会以为镜像里真的缺字体。
    const mine = ++generation.current
    return listFonts(includeBuiltin)
      .then((d) => {
        if (mine === generation.current) setData(d)
      })
      .catch((e: Error) => {
        if (mine === generation.current) setError(e.message)
      })
  }

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

  // 弹窗内点了「替换勾选的」或「这是新字体」都算 resolve；点「取消上传」
  // 走 handleConflictCancel，两者互斥，弹窗组件自己保证不会都触发。
  const handleConflictResolve = (replace: string[]) => {
    setConflict(null)
    conflictResolverRef.current?.(replace)
    conflictResolverRef.current = null
  }

  const handleConflictCancel = () => {
    setConflict(null)
    conflictResolverRef.current?.(null)
    conflictResolverRef.current = null
  }

  const waitForConflictResolution = (preflight: FontPreflight): Promise<string[] | null> =>
    new Promise((resolve) => {
      conflictResolverRef.current = resolve
      setConflict(preflight)
    })

  // 支持一次选多个文件，逐个处理：一个处理完（commit 或取消）才进下一个，
  // 弹窗永远只描述「当前这一个文件」的冲突，不会同时堆出好几个弹窗。
  const handleFilesSelected = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files ? Array.from(e.target.files) : []
    // 立刻清空 input value：不然选中同一个文件两次，第二次不会触发 onChange。
    e.target.value = ''
    if (files.length === 0) return

    setUploading(true)
    setError(null)
    setDuplicateNotice(null)
    try {
      for (const file of files) {
        try {
          const preflight = await preflightFont(file)
          if (preflight.duplicate_of) {
            setDuplicateNotice(
              t('admin.fonts.duplicate', { filename: preflight.duplicate_of.filename }),
            )
            continue
          }

          let replace: string[] = []
          if (preflight.candidates.length > 0) {
            const decision = await waitForConflictResolution(preflight)
            if (decision === null) continue // 这一个文件被取消上传
            replace = decision
          }

          await commitFont(preflight.token, replace)
          await reload(builtinExpanded)
        } catch (err) {
          setError((err as Error).message)
        }
      }
    } finally {
      setUploading(false)
    }
  }

  const renderRow = (font: FontFile) => (
    <div
      key={font.file_id}
      style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}
    >
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
      {duplicateNotice && (
        <p role="status" className="alert" style={{ margin: 0 }}>
          {duplicateNotice}
        </p>
      )}

      <div className="card glass" style={{ padding: 'var(--space-4)' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 'var(--space-2)',
            flexWrap: 'wrap',
          }}
        >
          <span className="section-title">{t('admin.fonts.managed')}</span>
          <button
            type="button"
            className="btn btn-primary"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            {uploading ? t('admin.fonts.uploading') : t('admin.fonts.upload')}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept=".ttf,.otf,.ttc"
            style={{ display: 'none' }}
            onChange={(e) => void handleFilesSelected(e)}
          />
        </div>
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
            aria-expanded={builtinExpanded}
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

      {conflict && (
        <FontConflictDialog
          preflight={conflict}
          onResolve={handleConflictResolve}
          onCancel={handleConflictCancel}
        />
      )}
    </>
  )
}
