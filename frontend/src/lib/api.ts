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

/** 失败响应读 body 的上限。正常情况下 body 与响应头同批到达，读取是纯
 *  内存操作（毫秒级），2 秒是极宽松的余量。 */
const ERROR_BODY_TIMEOUT_MS = 2000

/**
 * 分片上传专用的响应处理——**不能**换回通用的 parse()。
 *
 * iOS WebKit 上传一个 File.slice() 切出来的 Blob 时，响应**体**的读取
 * 永远不会完成：fetch 的 promise 在响应头到达时正常 resolve，紧接着的
 * resp.json() 就再也回不来了。真机实测（iPad OS 26 / WebKit 605.1.15）
 * 换 XMLHttpRequest 一样死——onload 也要等完整响应——所以这不是 fetch
 * 的问题，是网络层的，规避手段只有「不读 body」这一个。
 *
 * 症状极具误导性：服务端分片全部收齐、200 也发出去了、客户端 TCP 层
 * 连 ACK 都回了，但 JS 这边 Promise.all 永远不返回，complete 请求再也
 * 发不出去，界面就停在第一块不动。
 *
 * 所以成功路径一个字节都不读。putChunk 返回 void，本来也不需要响应内容。
 * 失败路径仍要给出可读的错误，但读 body 同样可能挂住，超时就退回状态码。
 */
async function parseChunkResponse(resp: Response): Promise<void> {
  if (resp.ok) return

  let code = 'INTERNAL_ERROR'
  let message = resp.statusText

  // 输掉的那个 promise 仍然挂着（fetch 没法取消已开始的 body 读取），
  // 但它不再挡住主流程——这正是这里用 race 而不是 await 的原因。
  const body = (await Promise.race([
    resp.json().catch(() => null),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), ERROR_BODY_TIMEOUT_MS)),
  ])) as { code?: string; message?: string } | null

  if (body) {
    code = body.code ?? code
    message = body.message ?? message
  }
  throw new ApiError(code, message, resp.status)
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
  await parseChunkResponse(resp)
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

// ---- 六期：账号 ----

export interface UserDto {
  user_id: string
  username: string
  email: string
  role: 'admin' | 'user'
  status: 'active' | 'suspended'
  created_at: string
}

export async function login(username: string, password: string): Promise<UserDto> {
  const resp = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  return parse<UserDto>(resp)
}

export async function logout(): Promise<void> {
  await fetch('/api/auth/logout', { method: 'POST' })
}

/**
 * 当前登录用户，未登录返回 null。
 *
 * 未登录是完全正常的状态（站点对匿名访客可用，只有 Graph 通道要求
 * 登录），所以后端用 200 + null 而不是 401 表达——用 401 会让浏览器
 * 控制台常态化地红一片，也会诱使这里把「正常的未登录」和「会话过期」
 * 写成同一条错误分支。
 */
export async function getMe(): Promise<UserDto | null> {
  const resp = await fetch('/api/auth/me')
  if (!resp.ok) return null
  return (await resp.json()) as UserDto | null
}

// ---- 六期：并发分块下载 ----

/**
 * 小于这个体积不并发。
 *
 * 原本定的是 4 MiB，按「小文件握手开销吃掉并行收益」的直觉拍的——但那是
 * 低延迟局域网下的直觉。真实部署是跨境链路（服务器在日本），实测：
 *
 *     单线程    60 秒收到 1.69 MB   28 KB/s
 *     4 路并发  60 秒收到 3.02 MB   50 KB/s   快 1.8 倍
 *
 * 跨境的单 TCP 连接受带宽延迟积限制，多连接能绕过去——这正是下载工具
 * 都用多线程的原因。而典型课件 PDF 就是 2-3 MB，全都卡在 4 MiB 下面，
 * 并发从来没被触发过。降到 512 KiB：这个体积下四次握手的代价（几百毫秒）
 * 相对高 RTT 链路的传输时间已经可以忽略。
 */
const CONCURRENT_MIN_BYTES = 512 * 1024
/**
 * 超过这个体积退回原生下载。并发必须先把整份 PDF 攒进内存才能拼成
 * Blob，而原生下载是流式的、不占页面内存。几百 MB 的产出宁可慢一点
 * 也不能让标签页 OOM。
 */
const CONCURRENT_MAX_BYTES = 200 * 1024 * 1024
/**
 * 并发连接数。
 *
 * 跨境实测（服务器在日本）：单线程 28 KB/s，4 路 50 KB/s。瓶颈是单条
 * TCP 连接的带宽延迟积，多开连接能绕过去。8 路是在「继续摊薄 RTT 影响」
 * 与「别把单机 uvicorn 的连接数吃光」之间取的值——自用场景同时只有一个
 * 人在下载，8 条连接对服务端毫无压力。
 */
export const CONCURRENCY = 8

export interface DownloadProgress {
  loaded: number
  total: number
}

/**
 * 并发分块下载，返回 Blob。后端的 FileResponse（Starlette）原生支持
 * Range 与 206 Partial Content，不需要额外端点。
 *
 * 相比原生下载多了一件事：进度可见。原生下载对大文件只有浏览器自己的
 * 进度条，页面上看不到，用户常以为按钮没反应。
 */
export async function downloadConcurrently(
  taskId: string,
  totalBytes: number,
  onProgress?: (p: DownloadProgress) => void,
): Promise<Blob> {
  const url = downloadUrl(taskId)
  const chunkSize = Math.ceil(totalBytes / CONCURRENCY)
  const ranges: Array<[number, number]> = []
  for (let start = 0; start < totalBytes; start += chunkSize) {
    ranges.push([start, Math.min(start + chunkSize, totalBytes) - 1])
  }

  let loaded = 0
  const parts = await Promise.all(
    ranges.map(async ([start, end]) => {
      const resp = await fetch(url, { headers: { Range: `bytes=${start}-${end}` } })
      if (resp.status !== 206) {
        throw new Error(`服务端未按 Range 响应（HTTP ${resp.status}）`)
      }
      const buf = await resp.arrayBuffer()
      loaded += buf.byteLength
      onProgress?.({ loaded, total: totalBytes })
      return buf
    }),
  )
  return new Blob(parts, { type: 'application/pdf' })
}

/**
 * 取下载体积。返回 null 表示拿不到（服务端没给 Content-Length），
 * 调用方据此退回原生下载。
 */
export async function getDownloadSize(taskId: string): Promise<number | null> {
  const head = await fetch(downloadUrl(taskId), { method: 'HEAD' })
  if (!head.ok) return null
  const len = head.headers.get('content-length')
  if (!len) return null
  const n = Number(len)
  return Number.isFinite(n) && n > 0 ? n : null
}

export function shouldDownloadConcurrently(totalBytes: number | null): boolean {
  if (totalBytes === null) return false
  return totalBytes >= CONCURRENT_MIN_BYTES && totalBytes <= CONCURRENT_MAX_BYTES
}

/** 把 Blob 存成文件。文件名由调用方给——Blob 没有服务端的 Content-Disposition。 */
export function saveBlob(blob: Blob, filename: string): void {
  const href = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = href
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  // 立刻 revoke 会让 Firefox 偶发拿不到内容，延后一拍
  setTimeout(() => URL.revokeObjectURL(href), 10_000)
}
