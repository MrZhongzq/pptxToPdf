import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TaskDto } from '../lib/api'
import { TaskCard } from './TaskCard'

const polling = vi.hoisted(() => ({
  state: { task: null as TaskDto | null, pollingTimedOut: false },
}))

vi.mock('../hooks/useTaskPolling', () => ({
  useTaskPolling: () => polling.state,
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

/** 后端 TaskDto.status 是裸 str，API 原样吐出——前端的映射表是唯一的
 *  兜底。少一个键就是 `STATUS[x].badge` 读 undefined，而仓库里没有
 *  ErrorBoundary，React 18 未捕获的渲染异常会卸载整棵树。 */
const ALL_STATUSES = [
  'pending',
  'parsing',
  'queued',
  'converting',
  'merging',
  'done',
  'failed',
] as const

describe('TaskCard 状态徽标', () => {
  beforeEach(() => {
    polling.state = { task: null, pollingTimedOut: false }
  })

  it.each(ALL_STATUSES)('渲染 %s 状态而不抛异常', (status) => {
    polling.state = { task: taskWith(status), pollingTimedOut: false }
    render(<TaskCard taskId="T1" />)
    expect(screen.getByText('deck.pptx')).toBeInTheDocument()
  })

  it('merging 有自己的中文文案，不是回落到状态码原文', () => {
    polling.state = { task: taskWith('merging'), pollingTimedOut: false }
    render(<TaskCard taskId="T1" />)
    expect(screen.getByText('合并中')).toBeInTheDocument()
  })

  it('merging 仍显示进行中进度条——它不是终态', () => {
    polling.state = { task: taskWith('merging'), pollingTimedOut: false }
    const { container } = render(<TaskCard taskId="T1" />)
    expect(container.querySelector('.sunken')).not.toBeNull()
    // 合并阶段还没有结果可下
    expect(screen.queryByRole('button', { name: /下载/ })).toBeNull()
  })
})
