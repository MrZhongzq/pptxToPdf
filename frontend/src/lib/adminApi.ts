export interface GraphCredentials {
  tenant_id: string
  client_id: string
  site_id: string
  drive_path: string
  secret_configured: boolean
}

export interface SelftestStep {
  step: string
  ok: boolean | null
  detail: string | null
}

export interface SelftestResult {
  ok: boolean
  steps: SelftestStep[]
}

export const STEP_LABELS: Record<string, string> = {
  token: '取 access token',
  drive: '访问中转库',
  upload: '上传测试文件',
  convert: '转换为 PDF',
  delete: '永久删除测试文件',
}

async function parseError(resp: Response): Promise<never> {
  const body = await resp.json().catch(() => ({}))
  const err = new Error(body.message || `HTTP ${resp.status}`) as Error & {
    code?: string
    steps?: SelftestStep[]
  }
  err.code = body.code
  err.steps = body.steps
  throw err
}

export async function login(password: string): Promise<void> {
  const resp = await fetch('/api/admin/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
  if (!resp.ok) await parseError(resp)
}

export async function logout(): Promise<void> {
  await fetch('/api/admin/logout', { method: 'POST' })
}

export async function getCredentials(): Promise<GraphCredentials> {
  const resp = await fetch('/api/admin/graph-credentials')
  if (!resp.ok) await parseError(resp)
  return resp.json()
}

export async function putCredentials(payload: {
  tenant_id: string
  client_id: string
  client_secret: string
  site_id: string
  drive_path: string
}): Promise<SelftestResult> {
  const resp = await fetch('/api/admin/graph-credentials', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!resp.ok) await parseError(resp)
  return resp.json()
}
