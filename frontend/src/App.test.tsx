import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { ApiError, type CapacityConfig } from './lib/api'

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
  startTask: vi.fn(),
}))

vi.mock('./lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./lib/api')>()
  return { ...actual, getCapacityConfig: mocks.getCapacityConfig, startTask: mocks.startTask }
})

vi.mock('./lib/uploadClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./lib/uploadClient')>()
  return { ...actual, uploadFile: mocks.uploadFile }
})

// TaskList/TaskCard 会自己发起轮询请求；App 层的这些测试只关心上传前的
// 预判提示与时机，不需要真的渲染任务卡片。渲染 taskIds 本身（而不是
// null）是为了让「start 成功/TASK_ALREADY_STARTED 后接入轮询」这类断言
// 有地方可查——不这样做就只能通过是否出现 TaskCard 的副作用去猜，猜不准。
vi.mock('./components/TaskList', () => ({
  TaskList: ({ taskIds }: { taskIds: string[] }) => (
    <div data-testid="task-ids">{taskIds.join(',')}</div>
  ),
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
    mocks.startTask.mockReset()
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

  it('选了风险文件出现确认提示后改选一个小文件：小文件正常上传、旧提示消失、引擎面板恢复可用（复审新发现的 bug）', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    selectGraphEngine()

    chooseFile(fileOfSize(300 * MIB))
    await screen.findByRole('button', { name: '仍然继续' })
    expect(screen.getByRole('button', { name: /Microsoft Graph/ })).toBeDisabled()

    // 改主意了：不理会这个提示，直接选另一个没有风险的小文件。
    chooseFile(fileOfSize(2 * MIB))

    await waitFor(() => expect(mocks.uploadFile).toHaveBeenCalledTimes(1))
    const [, opts] = mocks.uploadFile.mock.calls[0]
    expect(opts.engine).toBe('graph')

    // 旧提示不能还挂在屏幕上——它指向一个用户已经放弃的文件。
    expect(screen.queryByRole('button', { name: '仍然继续' })).toBeNull()
    expect(screen.queryByText(/建议改用 LibreOffice|大概率会在切片规划阶段/)).toBeNull()
    // 引擎面板不能被永久锁死。
    expect(screen.getByRole('button', { name: /Microsoft Graph/ })).not.toBeDisabled()
  })

  it('/admin 路径渲染管理页而不是上传界面', async () => {
    const original = window.location
    Object.defineProperty(window, 'location', {
      value: { ...original, pathname: '/admin' },
      writable: true,
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ code: 'ADMIN_UNAUTHORIZED' }), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
    render(<App />)
    expect(await screen.findByLabelText('管理口令')).toBeTruthy()
    Object.defineProperty(window, 'location', { value: original, writable: true })
  })
})

describe('App 两段式上传：ReadyCard 与「开始转换」', () => {
  beforeEach(() => {
    mocks.getCapacityConfig.mockReset().mockResolvedValue(CAPACITY)
    mocks.uploadFile.mockReset().mockResolvedValue({ taskId: 'T1' })
    mocks.startTask.mockReset()
  })

  async function uploadASmallFile() {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    chooseFile(fileOfSize(2 * MIB))
    await screen.findByText('deck.pptx')
  }

  it('上传完成后停在 ReadyCard，不直接开始轮询（变异检查 1：跳过 ReadyCard 直接轮询会让这里变红）', async () => {
    await uploadASmallFile()

    expect(screen.getByRole('button', { name: '开始转换' })).toBeInTheDocument()
    expect(screen.getByTestId('task-ids')).toHaveTextContent('')
  })

  it('点「开始转换」调用 startTask 并带上当前选的引擎（变异检查 3：不传 engine 会让这里变红）', async () => {
    mocks.startTask.mockResolvedValue({})
    await uploadASmallFile()

    fireEvent.click(screen.getByRole('button', { name: '开始转换' }))

    await waitFor(() =>
      expect(mocks.startTask).toHaveBeenCalledWith('T1', 'libreoffice', expect.anything()),
    )
    expect(screen.getByTestId('task-ids')).toHaveTextContent('T1')
  })

  it('READY_EXPIRED：展示后端给的原话，退回可以重新上传的状态', async () => {
    mocks.startTask.mockRejectedValue(
      new ApiError(
        'READY_EXPIRED',
        '上传后 1 小时内未开始转换，原文件已回收，请重新上传',
        410,
      ),
    )
    await uploadASmallFile()

    fireEvent.click(screen.getByRole('button', { name: '开始转换' }))

    expect(
      await screen.findByText('上传后 1 小时内未开始转换，原文件已回收，请重新上传'),
    ).toBeInTheDocument()
    // ReadyCard 不能停在原地——原文件已经没了，再点多少次都没用。
    expect(screen.queryByRole('button', { name: '开始转换' })).toBeNull()
    expect(screen.getByTestId('file-input')).toBeInTheDocument()
    expect(screen.getByTestId('task-ids')).toHaveTextContent('')
  })

  it('TASK_ALREADY_STARTED：任务是真的已经在跑（比如另一个标签页抢先点了），直接接入轮询而不是报错', async () => {
    mocks.startTask.mockRejectedValue(
      new ApiError('TASK_ALREADY_STARTED', '任务状态为 pending，无法重复启动', 409),
    )
    await uploadASmallFile()

    fireEvent.click(screen.getByRole('button', { name: '开始转换' }))

    await waitFor(() => expect(screen.getByTestId('task-ids')).toHaveTextContent('T1'))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('容量预判的决策点后移：在 ReadyCard 上把引擎换成 Graph 且命中风险时，点击确认前不发送任何 start 请求', async () => {
    render(<App />)
    await waitFor(() => expect(mocks.getCapacityConfig).toHaveBeenCalled())
    chooseFile(fileOfSize(300 * MIB))
    await screen.findByText('deck.pptx')

    fireEvent.click(screen.getByRole('button', { name: /Microsoft Graph/ }))

    await screen.findByRole('button', { name: '仍然继续' })
    expect(mocks.startTask).not.toHaveBeenCalled()

    mocks.startTask.mockResolvedValue({})
    fireEvent.click(screen.getByRole('button', { name: '仍然继续' }))

    await waitFor(() =>
      expect(mocks.startTask).toHaveBeenCalledWith('T1', 'graph', expect.anything()),
    )
  })
})
