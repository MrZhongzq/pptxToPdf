import { useEffect, useState } from 'react'
import { getTask, type TaskDto } from '../lib/api'

const POLL_MS = 1000
// 只有 done / failed 是终态。三期的 `merging` 看着像"快好了"，但它是
// 分片路径的中间态，加进来会让轮询停在"合并中"再也不刷新——合并几百 MB
// 的 deck 要跨好几个轮询周期，用户永远等不到下载按钮。
const TERMINAL = new Set<TaskDto['status']>(['done', 'failed'])
// 必须大于后端的 STALE_TASK_MINUTES（默认 45 分钟）——但比较的对象是
// "距任务内容上次变化的时长"，不是"挂载至今的总时长"。
//
// 三期 Task 7 的 C2 修复把后端的孤儿判据从"Task.updated_at 45 分钟未动"
// 改成了"该任务最新一片 TaskShard.updated_at 45 分钟未动"——也就是说，
// 后端本身早就不再假设"任务总墙钟 < 45 分钟"，它看的是活性（有没有在动），
// 不是总时长。一个健康的 12 片任务可以合法跑几个小时，只要分片还在陆续
// 转完。如果前端仍按"挂载至今"计时，46 分钟一到就会把仍在正常推进
// shard_done 的任务错判为已中断——见终审 finding F-1。
//
// 改成"距上次变化"之后 46 分钟不需要再加宽，但支撑它的是两条不同路径
// 各自的界，不是同一条不等式（复审更正）：
//
// - 分片路径：单个 convert_shard/merge job 的 RQ job_timeout
//   （convert_timeout_max_s + JOB_TIMEOUT_MARGIN_S，当前 1860s）封顶了
//   一次分片转换能跑多久，即两次 shard_done 变化之间的间隔上界。
//   1860s < MAX_POLL_MS(2760s)，余量 900s，很宽。
// - 非分片路径：孤儿回收器在 Task.updated_at 停止变化 stale_task_minutes
//   （默认 45 分钟 = 2700s）之后把任务标 failed，这本身就是一次内容
//   变化。2700s < MAX_POLL_MS(2760s)，余量只有 60s，很薄——但这条耦合
//   是二期就有的（MAX_POLL_MS 从一开始就只比 STALE_TASK_MINUTES 多 1
//   分钟），本轮把判据从"挂载至今"换成"距上次变化"没有让它变得更紧，
//   原样带过来而已。
//
// config.py 里 convert_timeout_max_s 的 docstring 记的
// `job_timeout < stale_task_minutes × 60` 是另一条不变量，保护的是
// merge 期间的终态翻转（回收器标 failed 之后 merge 又跑完改回 done），
// 跟这里前端 46 分钟阈值的依据是两回事，不要混用。
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
    // "距上次内容变化"，不是"挂载至今"——首次成功拉取也算一次变化
    // （null -> 第一份快照），所以初值用挂载时刻兜底即可。
    let lastChangedAt = Date.now()
    let lastSnapshot: string | null = null

    setTask(null)
    setPollingTimedOut(false)

    const poll = async () => {
      if (Date.now() - lastChangedAt > MAX_POLL_MS) {
        if (!cancelled) setPollingTimedOut(true)
        return
      }
      try {
        const next = await getTask(taskId)
        if (cancelled) return
        const snapshot = JSON.stringify(next)
        if (snapshot !== lastSnapshot) {
          lastSnapshot = snapshot
          lastChangedAt = Date.now()
        }
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
