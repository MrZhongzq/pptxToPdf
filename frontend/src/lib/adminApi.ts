import type { UserDto } from './api'
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


// ---- 六期：用户管理 ----


export async function listUsers(): Promise<UserDto[]> {
  const resp = await fetch('/api/admin/users')
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<UserDto[]>
}

export async function createUser(payload: {
  username: string
  email: string
  password: string
  role: 'admin' | 'user'
}): Promise<UserDto> {
  const resp = await fetch('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<UserDto>
}

export async function setUserStatus(
  userId: string,
  status: 'active' | 'suspended',
): Promise<UserDto> {
  const resp = await fetch(`/api/admin/users/${userId}/status`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    })
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<UserDto>
}

export async function setUserPassword(userId: string, password: string): Promise<UserDto> {
  const resp = await fetch(`/api/admin/users/${userId}/password`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    })
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<UserDto>
}

export async function deleteUser(userId: string): Promise<void> {
  const resp = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' })
  if (!resp.ok) await parseError(resp)
}

// ---- 六期：访问白名单 ----

export interface AllowedOrigin {
  origin_id: string
  origin: string
  note: string | null
  created_at: string
}

export async function listOrigins(): Promise<AllowedOrigin[]> {
  const resp = await fetch('/api/admin/origins')
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<AllowedOrigin[]>
}

export async function createOrigin(origin: string, note?: string): Promise<AllowedOrigin> {
  const resp = await fetch('/api/admin/origins', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ origin, note: note || null }),
    })
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<AllowedOrigin>
}

export async function deleteOrigin(originId: string): Promise<void> {
  const resp = await fetch(`/api/admin/origins/${originId}`, { method: 'DELETE' })
  if (!resp.ok) await parseError(resp)
}

// ---- 六期：系统状态 ----

export interface SystemStats {
  tasks_total: number
  tasks_by_status: Record<string, number>
  users_total: number
  storage_originals_bytes: number
  storage_outputs_bytes: number
  storage_shards_bytes: number
}

export async function getStats(): Promise<SystemStats> {
  const resp = await fetch('/api/admin/stats')
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<SystemStats>
}


// ---- 七期：黑名单 ----

export async function listBlocked(): Promise<AllowedOrigin[]> {
  const resp = await fetch('/api/admin/blocked')
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<AllowedOrigin[]>
}

export async function createBlocked(origin: string, note?: string): Promise<AllowedOrigin> {
  const resp = await fetch('/api/admin/blocked', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ origin, note: note || null }),
  })
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<AllowedOrigin>
}

export async function deleteBlocked(originId: string): Promise<void> {
  const resp = await fetch('/api/admin/blocked/' + originId, { method: 'DELETE' })
  if (!resp.ok) await parseError(resp)
}

// ---- 字体管理 ----

export interface FontFace {
  family: string
  style: string
}

export interface FontFile {
  file_id: string
  filename: string
  source: 'managed' | 'mounted' | 'builtin'
  /** 这个文件里包含的字体名。ttc 一个文件含多个，微软雅黑就是。 */
  families: string[]
  faces: FontFace[]
  version: string
  /** 覆盖的码位数。比版本号有用——版本号看不出子集化，字数能。 */
  charset_count: number
  size_bytes: number
  modified_at: string
  deletable: boolean
}

export interface FontList {
  managed: FontFile[]
  mounted: FontFile[]
  builtin: FontFile[]
}

export interface FontPreflight {
  token: string
  incoming: FontFile
  /** 非空表示这个文件一模一样地传过了，前端不该再弹冲突框。 */
  duplicate_of: FontFile | null
  candidates: FontFile[]
}

export async function listFonts(includeBuiltin = false): Promise<FontList> {
  const q = includeBuiltin ? '?include_builtin=true' : ''
  const resp = await fetch('/api/admin/fonts' + q)
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<FontList>
}

export async function preflightFont(file: File): Promise<FontPreflight> {
  const form = new FormData()
  form.append('file', file)
  const resp = await fetch('/api/admin/fonts/preflight', { method: 'POST', body: form })
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<FontPreflight>
}

export async function commitFont(token: string, replace: string[]): Promise<FontFile> {
  const resp = await fetch('/api/admin/fonts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, replace }),
  })
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<FontFile>
}

export async function deleteFont(fileId: string): Promise<void> {
  const resp = await fetch('/api/admin/fonts/' + fileId, { method: 'DELETE' })
  if (!resp.ok) await parseError(resp)
}
