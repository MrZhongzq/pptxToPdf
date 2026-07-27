import { useEffect, useState } from 'react'
import { getTask, type TaskDto } from '../lib/api'

const POLL_MS = 1000
// 只有 done / failed 是终态。三期的 `merging` 看着像"快好了"，但它是
// 分片路径的中间态，加进来会让轮询停在"合并中"再也不刷新——合并几百 MB
// 的 deck 要跨好几个轮询周期，用户永远等不到下载按钮。
const TERMINAL = new Set<TaskDto['status']>(['done', 'failed'])
// 必须大于后端的 STALE_TASK_MINUTES（默认 45 分钟），
// 否则前端会在后端把孤儿任务标 failed 之前就先放弃。
const MAX_POLL_MS = 46 * 60 * 1000

export interface TaskPollingState {
  task: TaskDto | null
  pollingTimedOut: boolean
}

export function useTaskPolling(taskId: string): TaskPollingState {
  const [task, setTask] = useState<TaskDto | null>(null)
  const [pollingTimedOut, setPollingTimedOut] = useState(false)

  useEffect(() => {
    let cancelled = false
    let timer: number
    const startedAt = Date.now()

    setTask(null)
    setPollingTimedOut(false)

    const poll = async () => {
      if (Date.now() - startedAt > MAX_POLL_MS) {
        if (!cancelled) setPollingTimedOut(true)
        return
      }
      try {
        const next = await getTask(taskId)
        if (cancelled) return
        setTask(next)
        if (!TERMINAL.has(next.status)) {
          timer = window.setTimeout(poll, POLL_MS)
        }
      } catch {
        if (!cancelled) timer = window.setTimeout(poll, POLL_MS * 3)
      }
    }

    void poll()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [taskId])

  return { task, pollingTimedOut }
}
