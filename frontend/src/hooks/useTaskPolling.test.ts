import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { TaskDto } from '../lib/api'
import * as api from '../lib/api'
import { useTaskPolling } from './useTaskPolling'

vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  getTask: vi.fn(),
}))

function taskWith(status: string): TaskDto {
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
    error_code: null,
    error_message: null,
    created_at: '2026-07-26T00:00:00Z',
  }
}

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
