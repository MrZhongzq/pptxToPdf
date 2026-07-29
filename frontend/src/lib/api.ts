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

export type EngineName = 'libreoffice' | 'graph'

/** 与后端 schemas.ConversionOptions 一一对应。后端目前只接收并落库，
 *  不实现任何一项——UI 上对应标注了「后端实现中」。 */
export interface ConversionOptions {
  expand_animations: boolean
  pdf_outline: boolean
  remap_margins: boolean
}

export const DEFAULT_OPTIONS: ConversionOptions = {
  expand_animations: false,
  pdf_outline: false,
  remap_margins: false,
}

export interface TaskDto {
  task_id: string
  /** 与后端 pipeline / shard_pipeline 写入的状态一一对应。`merging` 只出现在
   *  三期的分片路径上（N 片转完后合并），**不是终态**——加进
   *  useTaskPolling 的 TERMINAL 会让轮询停在"合并中"再也不刷新。 */
  status:
    | 'ready'
    | 'pending'
    | 'parsing'
    | 'queued'
    | 'converting'
    | 'merging'
    | 'done'
    | 'failed'
  engine: string
  original_filename: string
  size_bytes: number
  slide_count: number | null
  slide_width_emu: number | null
  slide_height_emu: number | null
  fonts: string[]
  options: ConversionOptions
  /** 切片总数，null 表示未切片（二期原路径）。与后端 schemas.TaskDto 对应。 */
  shard_total: number | null
  /** 已完成的分片数，由后端查询时现算；未切片任务恒为 0。 */
  shard_done: number
  error_code: string | null
  error_message: string | null
  created_at: string
}

/** 容量相关的只读配置，供选 Graph 引擎时做上传前的启发式预判。与后端
 *  schemas.CapacityConfig 一一对应，值由后端现读 app.config.settings 单例吐出。 */
export interface CapacityConfig {
  max_file_size: number
  graph_max_shards: number
  graph_max_shard_bytes: number
  graph_max_merge_bytes: number
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
  engine?: EngineName,
  options?: ConversionOptions,
): Promise<CreateUploadResponse> {
  const resp = await fetch('/api/uploads', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename, size, engine, options }),
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

/**
 * 把一个 ready 状态的任务真正送入转换队列。engine/options 都可选——不传
 * 就沿用上传时选的（后端 `if payload.engine is not None:` 才覆盖，见
 * app/api/tasks.py:start_task）。
 *
 * 两个已知的失败分支，调用方必须分开处理，不能都当成普通错误吞掉：
 * - 410 READY_EXPIRED：ready 任务有 1 小时 TTL，原文件已被回收，重试
 *   没有意义，只能引导用户重新上传。
 * - 409 TASK_ALREADY_STARTED：任务已经真的被启动过一次（比如另一个
 *   标签页抢先点了）——这种情况下任务是在正常转换，调用方该接上轮询，
 *   不是当成失败展示。
 */
export async function startTask(
  taskId: string,
  engine?: EngineName,
  options?: ConversionOptions,
): Promise<TaskDto> {
  const resp = await fetch(`/api/tasks/${taskId}/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ engine, options }),
  })
  return parse<TaskDto>(resp)
}

/** 不缓存——这几个数字来自后端 settings，四期上真实租户后可能被回调，
 *  每次都现取，不在前端另存一份可能漂移的副本。 */
export async function getCapacityConfig(): Promise<CapacityConfig> {
  return parse<CapacityConfig>(await fetch('/api/config/capacity'))
}

export function downloadUrl(taskId: string): string {
  return `/api/tasks/${taskId}/download`
}

/**
 * 下载前的轻量预检。通过则返回，调用方随后交给浏览器原生下载。
 *
 * 为什么不是 fetch + blob：那样浏览器必须把整个 PDF 读进内存才会弹出
 * 保存框。真实课件转出的 PDF 有 76MB，实测要等两分钟且全程没有任何
 * 进度提示，用户只会以为按钮没反应。原生下载是流式的，有浏览器自带的
 * 进度条与断点续传，也不占页面内存。
 *
 * HEAD 不传 body（Starlette 为 GET 路由自动注册 HEAD），毫秒级返回，
 * 只用来提前发现 410 RESULT_EXPIRED / 409 TASK_NOT_READY。HEAD 响应
 * 没有 body 拿不到错误码，所以失败后再发一次 GET 读详情——此时服务端
 * 返回的是几十字节的错误 JSON，不会真的下载文件。
 */
export async function preflightDownload(taskId: string): Promise<void> {
  const head = await fetch(downloadUrl(taskId), { method: 'HEAD' })
  if (head.ok) return

  const resp = await fetch(downloadUrl(taskId))
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

/**
 * 交给浏览器原生下载。不设 download 属性的文件名——让服务端的
 * Content-Disposition 决定，那边用的是 RFC 5987 编码，中文文件名才不会乱码。
 */
export function triggerNativeDownload(taskId: string): void {
  const link = document.createElement('a')
  link.href = downloadUrl(taskId)
  link.download = ''
  document.body.appendChild(link)
  link.click()
  link.remove()
}
