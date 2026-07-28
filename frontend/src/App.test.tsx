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
// 预判提示，不需要真的渲染任务卡片。
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

describe('App 上传前的容量启发式预判', () => {
  beforeEach(() => {
    mocks.getCapacityConfig.mockReset().mockResolvedValue(CAPACITY)
    // uploadFile 挂起不 resolve，模拟「文件已选中、还在上传」的窗口——
    // 这正是提示应该出现的时机（pendingFile 被设置之后）。
    mocks.uploadFile.mockReset().mockReturnValue(new Promise(() => {}))
  })

  it('未选择 Graph 引擎时，即使文件很大也不显示容量提示', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())

    chooseFile(fileOfSize(300 * MIB))

    expect(screen.queryByText(/Graph 通道/)).toBeNull()
  })

  it('选 Graph 引擎、文件很小：不显示任何提示', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()

    chooseFile(fileOfSize(1 * MIB))

    expect(screen.queryByText(/Graph 通道/)).toBeNull()
    expect(screen.queryByText(/建议改用 LibreOffice/)).toBeNull()
  })

  it('选 Graph 引擎、文件超过单片阈值但低于更严的合并预算：显示"会被切片/耗时更久"的软提示', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()

    // 40MiB < 100MiB <= 240MiB（更严的合并预算）
    chooseFile(fileOfSize(100 * MIB))

    expect(await screen.findByText(/切分后分批转换/)).toBeInTheDocument()
    expect(screen.queryByText(/建议改用 LibreOffice/)).toBeNull()
  })

  it('选 Graph 引擎、文件超过更严的那个卡点（合并预算 240MiB）：显示强风险提示，措辞诚实不假装精确', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()

    chooseFile(fileOfSize(300 * MIB))

    const warning = await screen.findByText(/建议改用 LibreOffice/)
    expect(warning).toBeInTheDocument()
    // 不假装能精确预测最终会不会失败
    expect(warning.textContent).not.toMatch(/\d+%/)
  })

  it('容量配置还没取到时不渲染任何容量提示（不假装知道阈值）', async () => {
    // 永不 resolve：模拟接口还没返回的窗口
    mocks.getCapacityConfig.mockReturnValue(new Promise(() => {}))
    render(<App />)
    selectGraphEngine()

    chooseFile(fileOfSize(300 * MIB))

    expect(screen.queryByText(/Graph 通道/)).toBeNull()
    expect(screen.queryByText(/建议改用 LibreOffice/)).toBeNull()
  })
})
