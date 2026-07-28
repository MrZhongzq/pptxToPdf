import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { CapacityConfig } from './lib/api'

const MIB = 1024 * 1024

const CAPACITY: CapacityConfig = {
  max_file_size: 600 * MIB,
  graph_max_shards: 12,
  graph_max_shard_bytes: 40 * MIB,
  graph_max_merge_bytes: 240 * MIB,
}

const mocks = vi.hoisted(() => ({
  getCapacityConfig: vi.fn(),
  uploadFile: vi.fn(),
}))

vi.mock('./lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./lib/api')>()
  return { ...actual, getCapacityConfig: mocks.getCapacityConfig }
})

vi.mock('./lib/uploadClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./lib/uploadClient')>()
  return { ...actual, uploadFile: mocks.uploadFile }
})

// TaskList/TaskCard 会自己发起轮询请求；App 层的这些测试只关心上传前的
// 预判提示与时机，不需要真的渲染任务卡片。
vi.mock('./components/TaskList', () => ({
  TaskList: () => null,
}))

function fileOfSize(size: number, name = 'deck.pptx'): File {
  const f = new File(['x'], name)
  Object.defineProperty(f, 'size', { value: size })
  return f
}

function selectGraphEngine() {
  fireEvent.click(screen.getByRole('button', { name: /Microsoft Graph/ }))
}

function chooseFile(file: File) {
  fireEvent.change(screen.getByTestId('file-input'), { target: { files: [file] } })
}

describe('App 上传前的容量启发式预判与确认时机', () => {
  beforeEach(() => {
    mocks.getCapacityConfig.mockReset().mockResolvedValue(CAPACITY)
    // uploadFile 挂起不 resolve，模拟"上传正在进行"的窗口，方便断言
    // 「是否已经发起过」而不受上传完成时序干扰。
    mocks.uploadFile.mockReset().mockReturnValue(new Promise(() => {}))
  })

  it('未选择 Graph 引擎时，即使文件很大也直接上传、不出现确认提示', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())

    chooseFile(fileOfSize(300 * MIB))

    expect(screen.queryByText(/Graph 通道/)).toBeNull()
    await waitFor(() => expect(mocks.uploadFile).toHaveBeenCalled())
  })

  it('选 Graph 引擎、文件很小：不出现确认提示，直接上传', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()

    chooseFile(fileOfSize(1 * MIB))

    expect(screen.queryByText(/Graph 通道/)).toBeNull()
    await waitFor(() => expect(mocks.uploadFile).toHaveBeenCalled())
  })

  it('验收核心：命中风险时，点击确认按钮之前绝不能发出任何上传请求（审查 Important #1）', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()

    chooseFile(fileOfSize(300 * MIB))

    await screen.findByRole('button', { name: '仍然继续' })
    // 提示（含两个操作按钮）已经出现，但这个时间点绝不能已经发出上传请求——
    // 否则用户在按钮还没点之前，300MB 已经传出去了。
    expect(mocks.uploadFile).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '仍然继续' }))

    await waitFor(() => expect(mocks.uploadFile).toHaveBeenCalled())
  })

  it('"仍然继续"：按用户当前选的 Graph 引擎发起上传', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()
    chooseFile(fileOfSize(300 * MIB))
    await screen.findByRole('button', { name: '仍然继续' })

    fireEvent.click(screen.getByRole('button', { name: '仍然继续' }))

    await waitFor(() => expect(mocks.uploadFile).toHaveBeenCalled())
    const [, opts] = mocks.uploadFile.mock.calls[0]
    expect(opts.engine).toBe('graph')
  })

  it('"改用 LibreOffice 并继续"：切换引擎、用 LibreOffice 发起上传，点击前没有发出任何上传请求', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()
    chooseFile(fileOfSize(300 * MIB))
    const switchBtn = await screen.findByRole('button', { name: /改用 LibreOffice/ })
    expect(mocks.uploadFile).not.toHaveBeenCalled()

    fireEvent.click(switchBtn)

    await waitFor(() => expect(mocks.uploadFile).toHaveBeenCalled())
    const [, opts] = mocks.uploadFile.mock.calls[0]
    expect(opts.engine).toBe('libreoffice')
    expect(screen.getByRole('button', { name: /Microsoft Graph/ })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })

  it('"shard" 档（超单片阈值但 <= 合并预算）：软提示不承诺具体秒数（审查 Important #2）', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()

    chooseFile(fileOfSize(100 * MIB))

    const msg = await screen.findByText(/切分后分批转换/)
    expect(msg.textContent).not.toMatch(/一分钟/)
    expect(msg.textContent).not.toMatch(/\d+\s*秒/)
  })

  it('"budget" 档（240MiB～480MiB）：措辞是"最终合并阶段"失败，不是"规划阶段"（审查 Minor #4）', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()

    chooseFile(fileOfSize(300 * MIB))

    const warning = await screen.findByText(/建议改用 LibreOffice/)
    expect(warning.textContent).toMatch(/合并/)
    expect(warning.textContent).not.toMatch(/规划/)
    expect(warning.textContent).not.toMatch(/\d+%/)
  })

  it('"reject" 档（超过 480MiB 分片总容量）：措辞是"规划阶段"就被拒，不是"合并阶段"（审查 Minor #4）', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()

    chooseFile(fileOfSize(500 * MIB))

    const warning = await screen.findByText(/规划阶段/)
    expect(warning.textContent).not.toMatch(/合并阶段/)
  })

  it('容量配置还没取到时不阻塞上传：按当前引擎直接上传，不假装知道阈值', async () => {
    mocks.getCapacityConfig.mockReturnValue(new Promise(() => {}))
    render(<App />)
    selectGraphEngine()

    chooseFile(fileOfSize(300 * MIB))

    expect(screen.queryByText(/Graph 通道/)).toBeNull()
    await waitFor(() => expect(mocks.uploadFile).toHaveBeenCalled())
  })

  it('容量端点的 max_file_size 真的参与上传前的大小上限判定（审查 Important #3）', async () => {
    mocks.getCapacityConfig.mockResolvedValue({ ...CAPACITY, max_file_size: 10 * MIB })
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())

    chooseFile(fileOfSize(20 * MIB))

    expect(await screen.findByRole('alert')).toHaveTextContent('超过上限')
    expect(mocks.uploadFile).not.toHaveBeenCalled()
  })
})
