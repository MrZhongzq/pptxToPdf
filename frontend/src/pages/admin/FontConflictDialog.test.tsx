import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '../../i18n'
import { FontConflictDialog } from './FontConflictDialog'

const incoming = {
  file_id: 'x', filename: 'msyh.ttc', source: 'managed' as const,
  families: ['微软雅黑'], faces: [{ family: '微软雅黑', style: 'Regular' }],
  version: '6.30', charset_count: 28762, size_bytes: 19_000_000,
  modified_at: '2026-08-03T00:00:00Z', deletable: true,
}
const managedCandidate = { ...incoming, file_id: 'old1', filename: 'msyh-old.ttc', version: '6.25' }
const mountedCandidate = {
  ...incoming, file_id: 'm1', filename: 'mounted.ttc',
  source: 'mounted' as const, deletable: false,
}
const builtinCandidate = {
  ...incoming, file_id: 'b1', filename: 'builtin.ttc',
  source: 'builtin' as const, deletable: false,
}

function renderDialog(candidates = [managedCandidate], onResolve = vi.fn(), onCancel = vi.fn()) {
  render(
    <I18nProvider>
      <FontConflictDialog
        preflight={{ token: 'tok', incoming, duplicate_of: null, candidates }}
        onResolve={onResolve}
        onCancel={onCancel}
      />
    </I18nProvider>,
  )
  return { onResolve, onCancel }
}

describe('FontConflictDialog', () => {
  it('lists every conflicting file with its own checkbox', () => {
    renderDialog([managedCandidate, { ...managedCandidate, file_id: 'old2', filename: 'b.ttc' }])
    expect(screen.getAllByRole('checkbox')).toHaveLength(2)
  })

  it('reports only the checked files as replacements', async () => {
    const { onResolve } = renderDialog([
      managedCandidate,
      { ...managedCandidate, file_id: 'old2', filename: 'b.ttc' },
    ])
    await userEvent.click(screen.getAllByRole('checkbox')[0])
    await userEvent.click(screen.getByRole('button', { name: /替换勾选|Replace selected/ }))
    expect(onResolve).toHaveBeenCalledWith(['old1'])
  })

  it('treats "this is a new font" as replacing nothing', async () => {
    const { onResolve } = renderDialog()
    await userEvent.click(screen.getByRole('button', { name: /新字体|new font/i }))
    expect(onResolve).toHaveBeenCalledWith([])
  })

  it('cancels without resolving', async () => {
    const { onResolve, onCancel } = renderDialog()
    await userEvent.click(screen.getByRole('button', { name: /取消|Cancel/ }))
    expect(onCancel).toHaveBeenCalled()
    expect(onResolve).not.toHaveBeenCalled()
  })

  it('does not let non-managed candidates be selected', () => {
    /** 手工挂载与内置的删不掉，列出来只是让管理员知道名字被占了。 */
    renderDialog([mountedCandidate])
    expect(screen.getByRole('checkbox')).toBeDisabled()
  })

  it('disables the replace button until something is checked', async () => {
    renderDialog()
    const btn = screen.getByRole('button', { name: /替换勾选|Replace selected/ })
    expect(btn).toBeDisabled()
    await userEvent.click(screen.getByRole('checkbox'))
    expect(btn).toBeEnabled()
  })

  it('does not warn when the coverage matches', () => {
    /** 字数掉一大截基本是子集化的精简版，换上去中文会缺字。
     *  这条提示是这个弹窗最有价值的部分。 */
    renderDialog([{ ...managedCandidate, charset_count: 28762 }])
    // incoming 也是 28762，字数相等，不该报警
    expect(screen.queryByText(/丢失|lose/i)).not.toBeInTheDocument()
  })

  it('shows how many characters would be lost', () => {
    // 候选（已有字体）36,220 字 > 上传的 28,762 字：勾选替换会把完整版
    // 覆盖成字少的版本，丢的字数是两者之差 7,458。
    renderDialog([{ ...managedCandidate, charset_count: 36220 }])
    expect(screen.getByText(/7,458/)).toBeInTheDocument()
  })

  it('gives mounted and builtin candidates different reasons for being locked', () => {
    // 都不可替换，但原因不同——builtin 显示"手工挂载"会误导管理员以为
    // 镜像里的 300 多个内置字体是谁手动放进去的。
    renderDialog([mountedCandidate, builtinCandidate])
    expect(screen.getByText(/手工挂载|Mounted manually/)).toBeInTheDocument()
    expect(screen.getByText(/镜像内置|Built into the image/)).toBeInTheDocument()
  })

  it('moves focus into the dialog when it opens', () => {
    renderDialog()
    expect(screen.getByRole('dialog')).toHaveFocus()
  })

  it('treats Escape the same as clicking cancel', async () => {
    // Esc 静默关闭会让 preflight 阶段已生成的暂存文件一直挂到 TTL 过期——
    // 所以 Esc 必须像点了「取消上传」一样调用 onCancel，不能什么都不做。
    const { onCancel, onResolve } = renderDialog()
    await userEvent.keyboard('{Escape}')
    expect(onCancel).toHaveBeenCalled()
    expect(onResolve).not.toHaveBeenCalled()
  })

  it('treats a click on the overlay the same as clicking cancel', () => {
    const { onCancel } = renderDialog()
    const overlay = document.querySelector('.glass-overlay') as HTMLElement
    fireEvent.mouseDown(overlay)
    expect(onCancel).toHaveBeenCalled()
  })

  it('does not cancel when the click originates inside the modal', () => {
    const { onCancel } = renderDialog()
    fireEvent.mouseDown(screen.getByRole('dialog'))
    expect(onCancel).not.toHaveBeenCalled()
  })
})
