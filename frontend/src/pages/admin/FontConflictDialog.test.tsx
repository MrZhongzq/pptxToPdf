import { render, screen } from '@testing-library/react'
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

  it('warns when the incoming font covers noticeably fewer characters', () => {
    /** 字数掉一大截基本是子集化的精简版，换上去中文会缺字。
     *  这条提示是这个弹窗最有价值的部分。 */
    renderDialog([{ ...managedCandidate, charset_count: 28762 }])
    // incoming 也是 28762，不该报警
    expect(screen.queryByText(/少了|fewer/)).not.toBeInTheDocument()
  })

  it('shows how many characters would be lost', () => {
    renderDialog([{ ...managedCandidate, charset_count: 36220 }])
    expect(screen.getByText(/7,458/)).toBeInTheDocument()
  })
})
