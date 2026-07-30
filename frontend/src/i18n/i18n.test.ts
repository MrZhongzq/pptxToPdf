import { describe, expect, it } from 'vitest'

import { availableLocales, detectLocale, translate } from './core'

describe('语言选择', () => {
  it('浏览器语言命中就用它', () => {
    expect(detectLocale(null, ['zh-CN'])).toBe('zh-CN')
  })

  it('只匹配主语言也算命中：zh-TW -> zh-CN', () => {
    // 繁体使用者读简体远比读英文顺畅，退到英文是过度保守
    expect(detectLocale(null, ['zh-TW'])).toBe('zh-CN')
  })

  it('小众语言回退英文，而不是中文', () => {
    // 需求明确：不在支持列表时回退英文。小众语言的使用者更可能读得懂
    // 英文，而不是一个他完全不认识的字符集。
    expect(detectLocale(null, ['is-IS'])).toBe('en')
    expect(detectLocale(null, ['sw'])).toBe('en')
  })

  it('一个都不认识时回退英文', () => {
    expect(detectLocale(null, [])).toBe('en')
  })

  it('存过的选择优先于浏览器语言', () => {
    expect(detectLocale('en', ['zh-CN'])).toBe('en')
  })

  it('存了一个不认识的语言时忽略它，回到浏览器语言', () => {
    expect(detectLocale('kl-GL', ['zh-CN'])).toBe('zh-CN')
  })

  it('按浏览器的偏好顺序取第一个命中的', () => {
    expect(detectLocale(null, ['is-IS', 'zh-CN', 'en'])).toBe('zh-CN')
  })
})

describe('取词', () => {
  it('取当前语言的文案', () => {
    expect(translate('zh-CN', 'ready.start')).toBe('开始转换')
    expect(translate('en', 'ready.start')).toBe('Start conversion')
  })

  it('当前语言缺这条时回退英文', () => {
    // 机器翻译的语言文件可能滞后于新增的 key
    expect(translate('zh-CN', 'nonexistent.key')).toBe('nonexistent.key')
  })

  it('两边都没有时返回 key 本身而不是空串', () => {
    // 界面上出现 "upload.title" 是一眼可见的缺翻译，空白只会让人以为
    // 布局坏了
    expect(translate('en', 'totally.missing')).toBe('totally.missing')
  })

  it('插值', () => {
    expect(translate('zh-CN', 'upload.dropzone.limit', { size: '600 MB' })).toContain('600 MB')
  })

  it('缺参数时保留占位符，不渲染成 undefined', () => {
    expect(translate('zh-CN', 'upload.dropzone.limit')).toContain('{size}')
  })
})

describe('语言文件完整性', () => {
  it('中英两份的 key 完全一致', async () => {
    const zh = (await import('./locales/zh-CN.json')).default
    const en = (await import('./locales/en.json')).default
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort())
  })

  it('至少含中英两门', () => {
    expect(availableLocales()).toEqual(expect.arrayContaining(['zh-CN', 'en']))
  })

  it('没有空文案', async () => {
    for (const code of availableLocales()) {
      const mod = (await import(`./locales/${code}.json`)).default as Record<string, string>
      for (const [key, value] of Object.entries(mod)) {
        expect(value, `${code} 的 ${key} 是空的`).toBeTruthy()
      }
    }
  })
})
