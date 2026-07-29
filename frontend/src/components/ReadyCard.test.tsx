import { useState } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { DEFAULT_OPTIONS, type ConversionOptions, type EngineName } from '../lib/api'
import { formatBytes } from '../lib/chunking'
import { ReadyCard } from './ReadyCard'

// ReadyCard 是受控组件——engine/options 由父组件（App）持有，测「换引擎
// 后再点」必须真的让状态变化反映到下一次点击，纯 props 不够，需要一个
// 持有 state 的外壳来模拟 App 的实际用法。
function Wrapper({
  onStart,
  initialEngine = 'libreoffice',
}: {
  onStart: (engine: EngineName, options: ConversionOptions) => Promise<void>
  initialEngine?: EngineName
}) {
  const [engine, setEngine] = useState<EngineName>(initialEngine)
  const [options, setOptions] = useState<ConversionOptions>(DEFAULT_OPTIONS)
  return (
    <ReadyCard
      filename="deck.pptx"
      sizeBytes={2 * 1024 * 1024}
      engine={engine}
      onEngineChange={setEngine}
      options={options}
      onOptionsChange={setOptions}
      onStart={onStart}
    />
  )
}

describe('ReadyCard', () => {
  it('显示文件名与体积', () => {
    render(<Wrapper onStart={vi.fn().mockResolvedValue(undefined)} />)

    expect(screen.getByText('deck.pptx')).toBeInTheDocument()
    expect(screen.getByText(formatBytes(2 * 1024 * 1024))).toBeInTheDocument()
  })

  it('点「开始转换」调用 onStart 并带上当前选的引擎', () => {
    const onStart = vi.fn().mockResolvedValue(undefined)
    render(<Wrapper onStart={onStart} initialEngine="graph" />)

    fireEvent.click(screen.getByRole('button', { name: '开始转换' }))

    expect(onStart).toHaveBeenCalledWith('graph', DEFAULT_OPTIONS)
  })

  it('开始转换请求进行中时禁用按钮', () => {
    // 挂起不 resolve——与四期自检按钮同理：不这样测就分不清是「请求发出
    // 前就禁用」还是「等响应回来才禁用」。
    const onStart = vi.fn().mockReturnValue(new Promise<void>(() => {}))
    render(<Wrapper onStart={onStart} />)

    fireEvent.click(screen.getByRole('button', { name: '开始转换' }))

    const button = screen.getByRole('button', { name: '启动中…' })
    expect(button).toBeDisabled()
  })

  it('引擎切换后再点，带的是新引擎', () => {
    const onStart = vi.fn().mockResolvedValue(undefined)
    render(<Wrapper onStart={onStart} initialEngine="libreoffice" />)

    fireEvent.click(screen.getByRole('button', { name: /Microsoft Graph/ }))
    fireEvent.click(screen.getByRole('button', { name: '开始转换' }))

    expect(onStart).toHaveBeenCalledWith('graph', DEFAULT_OPTIONS)
  })
})
