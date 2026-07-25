import { useEffect, useState } from 'react'
import { getTask, type TaskDto } from '../lib/api'

const POLL_MS = 1000
const TERMINAL = new Set(['done', 'failed'])

export function useTaskPolling(taskId: string): TaskDto | null {
  const [task, setTask] = useState<TaskDto | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: number

    const poll = async () => {
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

  return task
}
