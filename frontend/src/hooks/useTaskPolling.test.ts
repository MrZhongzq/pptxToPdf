import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TaskDto } from '../lib/api'
import * as api from '../lib/api'
import { useTaskPolling } from './useTaskPolling'

vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  getTask: vi.fn(),
}))

function taskWith(status: string, overrides: Partial<TaskDto> = {}): TaskDto {
  return {
    task_id: 'T1',
    status: status as TaskDto['status'],
    engine: 'graph',
    original_filename: 'deck.pptx',
    size_bytes: 1024,
    slide_count: 120,
    slide_width_emu: null,
    slide_height_emu: null,
    fonts: [],
    options: { expand_animations: false, pdf_outline: false, remap_margins: false },
    shard_total: null,
    shard_done: 0,
    error_code: null,
    error_message: null,
    created_at: '2026-07-26T00:00:00Z',
    ...overrides,
  }
}

describe('useTaskPolling 超时判据（终审 F-1：距上次内容变化，而非挂载至今）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('分片任务持续推进 shard_done，总时长超过 46 分钟也不判超时', async () => {
    // 旧判据（挂载至今）会在这里的第 46 分钟直接判超时——即便 shard_done
    // 一直在稳步递增、任务明显是活的。这正是终审 finding F-1 描述的失败：
    // 一个健康的分片任务被误判为"已中断"。
    const getTask = vi.mocked(api.getTask)
    getTask.mockReset()
    let shardDone = 0
    getTask.mockImplementation(async () =>
      taskWith('converting', { shard_total: 12, shard_done: shardDone }),
    )

    const { result } = renderHook(() => useTaskPolling('T1'))

    // 每 5 分钟推进一次分片进度，共 50 分钟——超过旧的 46 分钟挂载上限。
    for (let i = 0; i < 10; i++) {
      shardDone += 1
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5 * 60 * 1000)
      })
    }

    expect(result.current.pollingTimedOut).toBe(false)
    expect(result.current.task?.shard_done).toBe(10)
  }, 20000)

  it('任务内容 46 分钟一直没有变化，判定轮询超时', async () => {
    const getTask = vi.mocked(api.getTask)
    getTask.mockReset()
    getTask.mockResolvedValue(taskWith('converting'))

    const { result } = renderHook(() => useTaskPolling('T1'))

    // 分块推进（而不是一次性跳 47 分钟）：假定时器在一次巨量跳跃里不一定会
    // 把轮询过程中新调度出来的后续 setTimeout 都跟上，分块推进与上一条用例
    // 保持同样的推进粒度，规避这个问题。
    for (let i = 0; i < 48; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60 * 1000)
      })
    }

    expect(result.current.pollingTimedOut).toBe(true)
  }, 20000)
})

describe('useTaskPolling', () => {
  it('merging 不是终态，必须继续轮询', async () => {
    // 分片路径会经过 merging；把它当终态就会停在"合并中"再也不刷新，
    // 用户永远等不到下载按钮。
    const getTask = vi.mocked(api.getTask)
    getTask.mockReset()
    getTask
      .mockResolvedValueOnce(taskWith('merging'))
      .mockResolvedValue(taskWith('done'))

    const { result } = renderHook(() => useTaskPolling('T1'))

    await waitFor(() => expect(result.current.task?.status).toBe('merging'))
    await waitFor(() => expect(result.current.task?.status).toBe('done'), {
      timeout: 4000,
    })
    expect(getTask).toHaveBeenCalledTimes(2)
  })

  it('done 之后停止轮询', async () => {
    const getTask = vi.mocked(api.getTask)
    getTask.mockReset()
    getTask.mockResolvedValue(taskWith('done'))

    const { result } = renderHook(() => useTaskPolling('T1'))
    await waitFor(() => expect(result.current.task?.status).toBe('done'))

    await new Promise((r) => setTimeout(r, 1500))
    expect(getTask).toHaveBeenCalledTimes(1)
  })
})
