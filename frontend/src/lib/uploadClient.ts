import {
  completeUpload,
  createUpload,
  getUploadStatus,
  putChunk,
  type ConversionOptions,
  type EngineName,
} from './api'
import { backoffDelay, sliceChunks } from './chunking'

export type UploadPhase =
  | 'creating'
  | 'uploading'
  | 'completing'
  | 'done'
  | 'error'

export interface UploadProgress {
  bytesSent: number
  totalBytes: number
  chunksDone: number
  totalChunks: number
}

export interface UploadOptions {
  /** 用户选的转换引擎；省略则由后端 select_engine 自动判定 */
  engine?: EngineName
  /** 后处理选项，随创建会话的请求一起发给后端 */
  options?: ConversionOptions
  onProgress?: (p: UploadProgress) => void
  onPhase?: (phase: UploadPhase) => void
  signal?: AbortSignal
  concurrency?: number
  maxRetries?: number
  retryBaseMs?: number
  /** 续传已有会话；省略则新建 */
  resumeUploadId?: string
}

const DEFAULT_CONCURRENCY = 3
const DEFAULT_MAX_RETRIES = 3

function sleep(ms: number): Promise<void> {
  // backoffDelay(attempt) 在 attempt 为 NaN 时返回 NaN，传给 setTimeout 会
  // 退化成 0ms 忙循环。这里兜底成基础延迟，避免不受控的重试风暴。
  const safeMs = Number.isFinite(ms) ? ms : backoffDelay(0)
  return new Promise((resolve) => setTimeout(resolve, safeMs))
}

export async function uploadFile(
  file: File,
  opts: UploadOptions = {},
): Promise<{ taskId: string }> {
  const {
    engine,
    options,
    onProgress,
    onPhase,
    signal,
    concurrency = DEFAULT_CONCURRENCY,
    maxRetries = DEFAULT_MAX_RETRIES,
    retryBaseMs,
    resumeUploadId,
  } = opts

  try {
    onPhase?.('creating')

    // 续传时块大小必须取服务端的权威值。不能用 size/total_chunks 反推——
    // 最后一块通常不满，反推会得到错误的切分边界。
    let session: { upload_id: string; chunk_size: number; total_chunks: number }
    let already: Set<number>

    if (resumeUploadId) {
      const status = await getUploadStatus(resumeUploadId)
      session = {
        upload_id: resumeUploadId,
        chunk_size: status.chunk_size,
        total_chunks: status.total_chunks,
      }
      already = new Set(status.received_indices)
    } else {
      session = await createUpload(file.name, file.size, engine, options)
      already = new Set<number>()
    }

    const all = sliceChunks(file.size, session.chunk_size)
    const pending = all.filter((c) => !already.has(c.index))

    // bytesSent 永远不能超过 totalBytes：如果服务端的 chunk_size 和本地
    // 文件实际大小对不上，直接按本地切分算出的已收字节数逐个累加可能溢出，
    // 因此在这里夹紧到 file.size。
    let bytesSent = Math.min(
      all
        .filter((c) => already.has(c.index))
        .reduce((sum, c) => sum + (c.end - c.start), 0),
      file.size,
    )
    let chunksDone = already.size

    const emit = () =>
      onProgress?.({
        bytesSent,
        totalBytes: file.size,
        chunksDone,
        totalChunks: all.length,
      })

    onPhase?.('uploading')
    emit()

    let cursor = 0
    const worker = async (): Promise<void> => {
      while (cursor < pending.length) {
        if (signal?.aborted) throw new DOMException('aborted', 'AbortError')
        const chunk = pending[cursor]
        cursor += 1

        for (let attempt = 0; ; attempt += 1) {
          try {
            await putChunk(
              session.upload_id,
              chunk.index,
              file.slice(chunk.start, chunk.end),
              signal,
            )
            break
          } catch (err) {
            if (signal?.aborted || attempt >= maxRetries - 1) throw err
            await sleep(retryBaseMs ?? backoffDelay(attempt))
          }
        }

        bytesSent = Math.min(bytesSent + (chunk.end - chunk.start), file.size)
        chunksDone += 1
        emit()
      }
    }

    await Promise.all(
      Array.from({ length: Math.min(concurrency, pending.length || 1) }, worker),
    )

    onPhase?.('completing')
    const { task_id } = await completeUpload(session.upload_id)
    onPhase?.('done')
    return { taskId: task_id }
  } catch (err) {
    onPhase?.('error')
    throw err
  }
}
