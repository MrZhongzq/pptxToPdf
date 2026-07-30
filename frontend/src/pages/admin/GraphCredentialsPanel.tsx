import { useEffect, useState, type FormEvent } from 'react'
import {
  getCredentials,
  putCredentials,
  STEP_LABELS,
  type SelftestStep,
} from '../../lib/adminApi'

// 五步顺序固定，后端 STEPS 与此一致——即使 steps 数组顺序有变化也按
// 这个固定顺序渲染，避免 UI 顺序跟着一次意外的后端改动跳动。
const STEP_ORDER = ['token', 'drive', 'upload', 'convert', 'delete']

interface ApiErr extends Error {
  code?: string
  steps?: SelftestStep[]
}

function stepStatusText(ok: boolean | null): string {
  if (ok === true) return '通过'
  if (ok === false) return '失败'
  return '未执行'
}

function stepBadgeClass(ok: boolean | null): string {
  if (ok === true) return 'badge badge-success'
  if (ok === false) return 'badge badge-danger'
  return 'badge badge-neutral'
}


export function GraphCredentialsPanel() {
  const [tenantId, setTenantId] = useState('')
  const [clientId, setClientId] = useState('')
  const [siteId, setSiteId] = useState('')
  const [drivePath, setDrivePath] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [secretConfigured, setSecretConfigured] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [saving, setSaving] = useState(false)
  const [steps, setSteps] = useState<SelftestStep[] | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    getCredentials()
      .then((data) => {
        setTenantId(data.tenant_id)
        setClientId(data.client_id)
        setSiteId(data.site_id)
        setDrivePath(data.drive_path)
        setSecretConfigured(data.secret_configured)
      })
      .catch((err: unknown) => {
        const code = (err as ApiErr | undefined)?.code
        // 还没配过是正常状态，不是错误——首次进来就该是空表单
        if (code !== 'GRAPH_NOT_CONFIGURED') {
          setLoadError((err as ApiErr | undefined)?.message ?? '读取配置失败')
        }
      })
      .finally(() => setLoading(false))
  }, [])

  const handleSave = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setSaving(true)
    setSaveError(null)
    setSteps(null)
    try {
      const result = await putCredentials({
        tenant_id: tenantId,
        client_id: clientId,
        client_secret: clientSecret,
        site_id: siteId,
        drive_path: drivePath,
      })
      setSteps(result.steps)
      setSecretConfigured(true)
      setClientSecret('')
    } catch (err) {
      const apiErr = err as ApiErr
      if (apiErr.steps) setSteps(apiErr.steps)
      setSaveError(apiErr.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  // steps 优先用固定顺序渲染；后端返回的数组顺序理应一致，这里再兜底一次。
  const orderedSteps = steps
    ? STEP_ORDER.map((name) => steps.find((s) => s.step === name)).filter(
        (s): s is SelftestStep => s !== undefined,
      )
    : null

  if (loading) return <p style={{ color: 'var(--c-text-muted)' }}>加载中…</p>

  return (
    <form
      className="card glass"
      style={{ padding: 'var(--space-5)', display: 'grid', gap: 'var(--space-3)' }}
      onSubmit={handleSave}
    >
      <span className="section-title">Graph 凭证</span>
      {loadError && (
        <p role="alert" className="alert alert-danger">
          {loadError}
        </p>
      )}

      <div className="field">
        <label htmlFor="tenant-id">租户 ID</label>
        <input id="tenant-id" className="input" value={tenantId} onChange={(e) => setTenantId(e.target.value)} />
      </div>

      <div className="field">
        <label htmlFor="client-id">客户端 ID</label>
        <input id="client-id" className="input" value={clientId} onChange={(e) => setClientId(e.target.value)} />
      </div>

      <div className="field">
        <label htmlFor="client-secret">客户端密钥</label>
        <input
        id="client-secret"
        className="input"
        type="password"
        value={clientSecret}
        onChange={(e) => setClientSecret(e.target.value)}
        placeholder={secretConfigured ? '已配置（不回显），留空则沿用' : '首次配置必须填写'}
      />
      </div>
      {secretConfigured && <p className="check-hint">已配置（不回显），留空则沿用</p>}

      <div className="field">
        <label htmlFor="site-id">站点 ID</label>
        <input id="site-id" className="input" value={siteId} onChange={(e) => setSiteId(e.target.value)} />
      </div>

      <div className="field">
        <label htmlFor="drive-path">云盘路径</label>
        <input id="drive-path" className="input" value={drivePath} onChange={(e) => setDrivePath(e.target.value)} />
      </div>

      <button type="submit" className="btn btn-primary" disabled={saving}>
        {saving ? '自检中…' : '测试并保存'}
      </button>

      {saveError && (
        <p role="alert" className="alert alert-danger">
          {saveError}
        </p>
      )}

      {orderedSteps && (
        <ul>
          {orderedSteps.map((s) => (
            <li key={s.step}>
              <span>{STEP_LABELS[s.step] ?? s.step}</span>
              <span className={stepBadgeClass(s.ok)}>{stepStatusText(s.ok)}</span>
              {s.ok === false && s.detail && <p>{s.detail}</p>}
            </li>
          ))}
        </ul>
      )}
    </form>
  )
}
