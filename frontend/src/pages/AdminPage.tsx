import { useEffect, useState, type FormEvent } from 'react'
import {
  getCredentials,
  login,
  logout,
  putCredentials,
  STEP_LABELS,
  type SelftestStep,
} from '../lib/adminApi'

type AuthState = 'checking' | 'unconfigured' | 'unauthed' | 'authed'

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

export function AdminPage() {
  const [authState, setAuthState] = useState<AuthState>('checking')

  const [loginPassword, setLoginPassword] = useState('')
  const [loginError, setLoginError] = useState<string | null>(null)
  const [loggingIn, setLoggingIn] = useState(false)

  const [tenantId, setTenantId] = useState('')
  const [clientId, setClientId] = useState('')
  const [siteId, setSiteId] = useState('')
  const [drivePath, setDrivePath] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [secretConfigured, setSecretConfigured] = useState(false)

  const [saving, setSaving] = useState(false)
  const [steps, setSteps] = useState<SelftestStep[] | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const loadCredentials = async () => {
    const data = await getCredentials()
    setTenantId(data.tenant_id)
    setClientId(data.client_id)
    setSiteId(data.site_id)
    setDrivePath(data.drive_path)
    setSecretConfigured(data.secret_configured)
    setClientSecret('')
    setAuthState('authed')
  }

  useEffect(() => {
    loadCredentials().catch((err: unknown) => {
      const code = (err as ApiErr | undefined)?.code
      setAuthState(code === 'ADMIN_NOT_CONFIGURED' ? 'unconfigured' : 'unauthed')
    })
  }, [])

  const handleLogin = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setLoginError(null)
    setLoggingIn(true)
    try {
      await login(loginPassword)
      await loadCredentials()
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoggingIn(false)
    }
  }

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
    } catch (err) {
      const apiErr = err as ApiErr
      if (apiErr.steps) setSteps(apiErr.steps)
      setSaveError(apiErr.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handleLogout = async () => {
    await logout()
    setAuthState('unauthed')
  }

  if (authState === 'checking') {
    return (
      <div className="layout">
        <p>加载中…</p>
      </div>
    )
  }

  if (authState === 'unconfigured') {
    return (
      <div className="layout">
        <header className="page-head">
          <h1 className="page-title">管理入口</h1>
        </header>
        <p role="alert" className="alert alert-danger">
          管理入口未配置口令
        </p>
      </div>
    )
  }

  if (authState === 'unauthed') {
    return (
      <div className="layout">
        <header className="page-head">
          <h1 className="page-title">管理入口</h1>
        </header>
        <form className="card" style={{ padding: 'var(--space-4)' }} onSubmit={handleLogin}>
          <label htmlFor="admin-password">管理口令</label>
          <input
            id="admin-password"
            type="password"
            value={loginPassword}
            onChange={(e) => setLoginPassword(e.target.value)}
          />
          <button type="submit" className="btn btn-primary" disabled={loggingIn}>
            {loggingIn ? '登录中…' : '登录'}
          </button>
          {loginError && (
            <p role="alert" className="alert alert-danger">
              {loginError}
            </p>
          )}
        </form>
      </div>
    )
  }

  // authState === 'authed'
  // steps 优先用固定顺序渲染；后端返回的数组顺序理应一致，这里再兜底一次。
  const orderedSteps = steps
    ? STEP_ORDER.map((name) => steps.find((s) => s.step === name)).filter(
        (s): s is SelftestStep => s !== undefined,
      )
    : null

  return (
    <div className="layout">
      <header className="page-head">
        <h1 className="page-title">Graph 凭证配置</h1>
        <button type="button" className="btn btn-ghost" onClick={() => void handleLogout()}>
          登出
        </button>
      </header>
      <form className="card" style={{ padding: 'var(--space-4)' }} onSubmit={handleSave}>
        <label htmlFor="tenant-id">租户 ID</label>
        <input id="tenant-id" value={tenantId} onChange={(e) => setTenantId(e.target.value)} />

        <label htmlFor="client-id">客户端 ID</label>
        <input id="client-id" value={clientId} onChange={(e) => setClientId(e.target.value)} />

        <label htmlFor="client-secret">客户端密钥</label>
        <input
          id="client-secret"
          type="password"
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          placeholder={secretConfigured ? '已配置（不回显），留空则沿用' : '首次配置必须填写'}
        />
        {secretConfigured && <p className="check-hint">已配置（不回显），留空则沿用</p>}

        <label htmlFor="site-id">站点 ID</label>
        <input id="site-id" value={siteId} onChange={(e) => setSiteId(e.target.value)} />

        <label htmlFor="drive-path">云盘路径</label>
        <input id="drive-path" value={drivePath} onChange={(e) => setDrivePath(e.target.value)} />

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
    </div>
  )
}
