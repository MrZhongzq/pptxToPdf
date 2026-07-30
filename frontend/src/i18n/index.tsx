import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import en from './locales/en.json'
import zhCN from './locales/zh-CN.json'

/**
 * 极简 i18n。
 *
 * 没有引入 react-i18next：这个项目需要的只是「按浏览器语言选一份 JSON、
 * 取不到就回退英文」，而那个库带来的复数规则、命名空间、懒加载、后端
 * 适配器一个都用不上，却要多背 40 KB 和一套自己的配置心智。
 *
 * 语言文件是扁平的点分 key，正是 GitHub Actions 上那些翻译 bot 认得的
 * 格式——机器翻译的语言直接由 CI 从 en.json 生成，不进人工维护清单。
 */

// 人工维护的两门。其余语言由 CI 机器翻译生成，见 scripts/translate.mjs
const SOURCE_LOCALES = { 'zh-CN': zhCN, en } as const

export type Messages = typeof en
export type Locale = string

const FALLBACK: Locale = 'en'
const STORAGE_KEY = 'pptx2pdf_locale'

// 机器翻译产出的语言在构建时被 Vite 的 glob 收进来，缺了也不影响运行
const generated = import.meta.glob<{ default: Messages }>('./locales/*.json', {
  eager: true,
})

function allMessages(): Record<string, Partial<Messages>> {
  const out: Record<string, Partial<Messages>> = {}
  for (const [path, mod] of Object.entries(generated)) {
    const code = path.replace('./locales/', '').replace('.json', '')
    out[code] = mod.default
  }
  return { ...out, ...SOURCE_LOCALES }
}

const MESSAGES = allMessages()

export function availableLocales(): string[] {
  return Object.keys(MESSAGES).sort()
}

/**
 * 选定语言。
 *
 * 依次尝试：存过的选择 → 浏览器语言的完整标签（zh-CN）→ 主语言（zh）
 * → 英文。**回退到英文而不是中文**是需求明确要求的：小众语言的使用者
 * 更可能读得懂英文，而不是一个他完全不认识的字符集。
 */
export function detectLocale(
  stored?: string | null,
  languages: readonly string[] = typeof navigator !== 'undefined' ? navigator.languages ?? [] : [],
): Locale {
  if (stored && MESSAGES[stored]) return stored
  for (const raw of languages) {
    if (MESSAGES[raw]) return raw
    const primary = raw.split('-')[0]
    const hit = Object.keys(MESSAGES).find((code) => code.split('-')[0] === primary)
    if (hit) return hit
  }
  return FALLBACK
}

function lookup(locale: Locale, key: string): string | undefined {
  const table = MESSAGES[locale] as Record<string, string> | undefined
  return table?.[key]
}

export function translate(locale: Locale, key: string, params?: Record<string, unknown>): string {
  // 逐级回退：当前语言 -> 英文 -> key 本身。
  // 返回 key 而不是空串：界面上出现 "upload.title" 是一眼可见的缺翻译，
  // 而空白只会让人以为布局坏了。
  const raw = lookup(locale, key) ?? lookup(FALLBACK, key) ?? key
  if (!params) return raw
  return raw.replace(/\{(\w+)\}/g, (m, name) =>
    params[name] === undefined ? m : String(params[name]),
  )
}

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
