import { render as rtlRender, type RenderOptions } from '@testing-library/react'
import { beforeEach } from 'vitest'
import type { ReactElement, ReactNode } from 'react'

import { I18nProvider } from './i18n'

/**
 * 套上应用级 Provider 的 render。
 *
 * useI18n 在缺少 Provider 时**故意抛错**而不是静默退化成英文——那样
 * 「忘了套 Provider」在开发时毫无症状，直到某个页面整片变英文才被发现。
 * 代价是测试也必须套，所以统一从这里 render。
 */
function AllProviders({ children }: { children: ReactNode }) {
  return <I18nProvider>{children}</I18nProvider>
}

/**
 * 测试统一跑中文。
 *
 * 断言里写的是中文文案，而 jsdom 的 navigator.language 是 en-US——不锁
 * 语言的话，同一份测试在开发机和 CI 上会因为环境语言不同而给出不同结果。
 * 锁在这里而不是每个文件各写一遍。
 */
beforeEach(() => {
  localStorage.setItem('pptx2pdf_locale', 'zh-CN')
})

export function render(ui: ReactElement, options?: Omit<RenderOptions, 'wrapper'>) {
  return rtlRender(ui, { wrapper: AllProviders, ...options })
}

export * from '@testing-library/react'
