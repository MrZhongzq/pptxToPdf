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

/** 后端 TaskDto.status 是裸 str，API 原样吐出——前端的映射表是唯一的
 *  兜底。少一个键就是 `STATUS[x].badge` 读 undefined，而仓库里没有
 *  ErrorBoundary，React 18 未捕获的渲染异常会卸载整棵树。 */
const ALL_STATUSES = [
  'ready',
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

describe('TaskCard pollingTimedOut（终审 F-2：此前全部 13 处用例都传 false，零覆盖）', () => {
  beforeEach(() => {
    polling.state = { task: null, pollingTimedOut: false }
  })

  it('pollingTimedOut 为 true 时显示超时文案，替换掉整张卡片', () => {
    polling.state = { task: null, pollingTimedOut: true }
    render(<TaskCard taskId="T1" />)

    expect(
      screen.getByText('任务状态长时间未更新，可能已中断。请重新上传。'),
    ).toBeInTheDocument()
    // 是替换而不是叠加：文件名等正常卡片内容不应该同时出现
    expect(screen.queryByText('deck.pptx')).toBeNull()
  })

  it('任务仍在推进（有 task 数据、状态非终态）时，pollingTimedOut 为 false 不出现超时文案', () => {
    polling.state = {
      task: taskWith('converting', { shard_total: 8, shard_done: 3 }),
      pollingTimedOut: false,
    }
    render(<TaskCard taskId="T1" />)

    expect(
      screen.queryByText('任务状态长时间未更新，可能已中断。请重新上传。'),
    ).toBeNull()
    expect(screen.getByText('deck.pptx')).toBeInTheDocument()
  })
})

describe('TaskCard 分片进度（长耗时任务的紫色区分）', () => {
  beforeEach(() => {
    polling.state = { task: null, pollingTimedOut: false }
  })

  it('未分片任务（shard_total 为 null）不显示分片进度文案，也没有紫色边框', () => {
    const task = taskWith('converting', { shard_total: null, shard_done: 0 })
    polling.state = { task, pollingTimedOut: false }
    const { container } = render(<TaskCard taskId="T1" />)

    expect(screen.queryByText(/已完成.*片/)).toBeNull()
    const card = container.querySelector('.card') as HTMLElement
    expect(card.style.borderLeft).toBe('')
  })

  it('shard_total 为 0（非 null 但无效）同样不算分片任务', () => {
    const task = taskWith('converting', { shard_total: 0, shard_done: 0 })
    polling.state = { task, pollingTimedOut: false }
    const { container } = render(<TaskCard taskId="T1" />)

    expect(screen.queryByText(/已完成.*片/)).toBeNull()
    const card = container.querySelector('.card') as HTMLElement
    expect(card.style.borderLeft).toBe('')
  })

  it('分片任务（shard_total 有值）进行中：显示已完成分片数与紫色边框', () => {
    const task = taskWith('converting', { shard_total: 8, shard_done: 3 })
    polling.state = { task, pollingTimedOut: false }
    const { container } = render(<TaskCard taskId="T1" />)

    expect(screen.getByText('已完成 3 / 8 片')).toBeInTheDocument()
    const card = container.querySelector('.card') as HTMLElement
    expect(card.style.borderLeft).toBe('4px solid var(--c-notable)')
  })

  it('分片任务的进度条宽度按 shard_done / shard_total 四舍五入换算成百分比', () => {
    const task = taskWith('merging', { shard_total: 3, shard_done: 1 })
    polling.state = { task, pollingTimedOut: false }
    const { container } = render(<TaskCard taskId="T1" />)

    // 1/3 = 33.33...% -> round 到 33%
    const bar = container.querySelector('.sunken > div') as HTMLElement
    expect(bar.style.width).toBe('33%')
  })

  it('shard_done 为 0 时用不定长动画而不是纹丝不动的空槽（审查 Minor #5）', () => {
    // 第一片可能要转好几分钟，静态的 0% 宽度槽看起来比普通任务的
    // indeterminate 动画更像"卡死"——这与用户「不要让我觉得没反应」的
    // 诉求正相反，必须保留动感。
    const task = taskWith('converting', { shard_total: 8, shard_done: 0 })
    polling.state = { task, pollingTimedOut: false }
    const { container } = render(<TaskCard taskId="T1" />)

    const bar = container.querySelector('.sunken > div') as HTMLElement
    expect(bar.style.animation).toContain('indeterminate')
  })

  it('分片任务已完成（done）：不再显示分片进度条/文案，但保留紫色边框', () => {
    const task = taskWith('done', { shard_total: 8, shard_done: 8 })
    polling.state = { task, pollingTimedOut: false }
    const { container } = render(<TaskCard taskId="T1" />)

    expect(screen.queryByText(/已完成.*片/)).toBeNull()
    const card = container.querySelector('.card') as HTMLElement
    expect(card.style.borderLeft).toBe('4px solid var(--c-notable)')
  })
})
