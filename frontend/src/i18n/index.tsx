/* eslint-disable react-refresh/only-export-components -- Provider 与配套的 useI18n
   放在一起是 React Context 的标准写法；把 hook 拆到另一个文件只会让每个
   调用点多一行 import，换不来任何东西。纯逻辑已经拆到 core.ts 了。 */
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { detectLocale, translate, type Locale } from './core'

const STORAGE_KEY = 'pptx2pdf_locale'

interface I18nValue {
  locale: Locale
  setLocale: (l: Locale) => void
  t: (key: string, params?: Record<string, unknown>) => string
}

const I18nContext = createContext<I18nValue | null>(null)

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() =>
    detectLocale(typeof localStorage !== 'undefined' ? localStorage.getItem(STORAGE_KEY) : null),
  )

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // 隐私模式下 localStorage 会抛——记不住选择而已，不该让界面崩
    }
  }, [])

  const value = useMemo<I18nValue>(
    () => ({ locale, setLocale, t: (k, p) => translate(locale, k, p) }),
    [locale, setLocale],
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext)
  if (ctx === null) {
    // 不静默退化成英文：那会让「忘了套 Provider」在开发时毫无症状，
    // 直到某个页面整片变英文才被发现。
    throw new Error('useI18n 必须在 <I18nProvider> 内使用')
  }
  return ctx
}
