import { useEffect, useState } from 'react'
import { getTask, type TaskDto } from '../lib/api'

const POLL_MS = 1000
const TERMINAL = new Set(['done', 'failed'])
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
