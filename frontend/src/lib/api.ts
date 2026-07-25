export interface CreateUploadResponse {
  upload_id: string
  chunk_size: number
  total_chunks: number
  expires_at: string
}

export interface UploadStatusResponse {
  received_indices: number[]
  bytes_received: number
  total_chunks: number
  chunk_size: number
  status: string
}

export interface TaskDto {
  task_id: string
  status: 'pending' | 'parsing' | 'queued' | 'converting' | 'done' | 'failed'
  engine: string
  original_filename: string
  size_bytes: number
  slide_count: number | null
  slide_width_emu: number | null
  slide_height_emu: number | null
  fonts: string[]
  error_code: string | null
  error_message: string | null
  created_at: string
}

export class ApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(code: string, message: string, status: number) {
    super(message)
    this.code = code
    this.status = status
  }
}

async function parse<T>(resp: Response): Promise<T> {
  if (resp.ok) return (await resp.json()) as T
  let code = 'INTERNAL_ERROR'
  let message = resp.statusText
  try {
    const body = await resp.json()
    code = body.code ?? code
    message = body.message ?? message
  } catch {
    // 响应体不是 JSON，保留状态文本
  }
  throw new ApiError(code, message, resp.status)
}

export async function createUpload(
  filename: string,
  size: number,
): Promise<CreateUploadResponse> {
  const resp = await fetch('/api/uploads', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename, size }),
  })
  return parse<CreateUploadResponse>(resp)
}

export async function putChunk(
  uploadId: string,
  index: number,
  blob: Blob,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`/api/uploads/${uploadId}/chunks/${index}`, {
    method: 'PUT',
    body: blob,
    signal,
  })
  await parse<unknown>(resp)
}

export async function getUploadStatus(
  uploadId: string,
): Promise<UploadStatusResponse> {
  return parse<UploadStatusResponse>(await fetch(`/api/uploads/${uploadId}`))
}

export async function completeUpload(
  uploadId: string,
): Promise<{ task_id: string }> {
  const resp = await fetch(`/api/uploads/${uploadId}/complete`, {
    method: 'POST',
  })
  return parse<{ task_id: string }>(resp)
}

export async function getTask(taskId: string): Promise<TaskDto> {
  return parse<TaskDto>(await fetch(`/api/tasks/${taskId}`))
}

export function downloadUrl(taskId: string): string {
  return `/api/tasks/${taskId}/download`
}

/**
 * 拉取转换结果文件的原始 Response，不解析成 JSON——调用方需要
 * response.blob() 触发浏览器下载，以及读 Content-Disposition 拿文件名。
 * 失败时（410 RESULT_EXPIRED / 409 TASK_NOT_READY 等）复用 parse() 同款
 * 错误归一化逻辑，抛 ApiError 而不是把裸 JSON 错误体交给调用方当文件下载。
 */
export async function fetchTaskFile(taskId: string): Promise<Response> {
  const resp = await fetch(downloadUrl(taskId))
  if (resp.ok) return resp
  let code = 'INTERNAL_ERROR'
  let message = resp.statusText
  try {
    const body = await resp.json()
    code = body.code ?? code
    message = body.message ?? message
  } catch {
    // 响应体不是 JSON，保留状态文本
  }
  throw new ApiError(code, message, resp.status)
}
