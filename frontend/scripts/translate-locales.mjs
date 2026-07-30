#!/usr/bin/env node
/**
 * 从 en.json 机器翻译出其余语言。
 *
 * 只翻 en.json 而不是 zh-CN.json：英文是源语言里最不容易产生歧义的一份，
 * 而且几乎所有翻译服务对 en->X 的质量都好过 zh->X。
 *
 * 中英两门是人工维护的，本脚本**不会覆盖它们**。
 *
 * 用法：
 *   DEEPL_API_KEY=xxx node scripts/translate-locales.mjs
 *   node scripts/translate-locales.mjs --check      只校验，不写文件
 *
 * 没有 API key 时退化成「补齐缺失的 key、值直接用英文原文」——CI 里
 * 没配密钥也不会失败，界面上看到英文总好过看到 key。
 */
import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const LOCALES = join(HERE, '..', 'src', 'i18n', 'locales')

// 人工维护，脚本不碰
const SOURCE = new Set(['en', 'zh-CN'])

// 机器翻译的目标语言。加一门就在这里加一行——它们不进人工维护清单。
const TARGETS = ['ja', 'ko', 'es', 'fr', 'de', 'ru', 'pt-BR']

const checkOnly = process.argv.includes('--check')
const apiKey = process.env.DEEPL_API_KEY

const en = JSON.parse(readFileSync(join(LOCALES, 'en.json'), 'utf8'))
const keys = Object.keys(en)

/** DeepL 的语言代码与 BCP 47 不完全一致 */
function deeplCode(locale) {
  const map = { 'pt-BR': 'PT-BR', ja: 'JA', ko: 'KO', es: 'ES', fr: 'FR', de: 'DE', ru: 'RU' }
  return map[locale] ?? locale.toUpperCase()
}

async function translateBatch(texts, target) {
  if (!apiKey) return texts // 无 key：原样返回英文
  const res = await fetch('https://api-free.deepl.com/v2/translate', {
    method: 'POST',
    headers: {
      Authorization: `DeepL-Auth-Key ${apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      text: texts,
      source_lang: 'EN',
      target_lang: deeplCode(target),
      // 占位符 {name} 不能被翻译动过，否则插值会静默失效
      ignore_tags: ['x'],
      tag_handling: 'xml',
    }),
  })
  if (!res.ok) {
    throw new Error(`DeepL ${res.status}: ${await res.text()}`)
  }
  const body = await res.json()
  return body.translations.map((t) => t.text)
}

/** 把 {name} 包成 <x> 标签，翻译服务就不会动它 */
const protect = (s) => s.replace(/\{(\w+)\}/g, '<x>{$1}</x>')
const unprotect = (s) => s.replace(/<x>\{(\w+)\}<\/x>/g, '{$1}').replace(/<\/?x>/g, '')

let failed = false

for (const target of TARGETS) {
  const path = join(LOCALES, `${target}.json`)
  const existing = existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : {}
  const missing = keys.filter((k) => !existing[k])
  const stale = Object.keys(existing).filter((k) => !en[k])

  if (checkOnly) {
    if (missing.length || stale.length) {
      console.error(
        `${target}: 缺 ${missing.length} 条，多余 ${stale.length} 条 —— 跑一次 translate-locales.mjs`,
      )
      failed = true
    } else {
      console.log(`${target}: 与 en.json 对齐`)
    }
    continue
  }

  const out = {}
  if (missing.length) {
    const translated = await translateBatch(missing.map((k) => protect(en[k])), target)
    missing.forEach((k, i) => {
      out[k] = unprotect(translated[i])
    })
    console.log(`${target}: 新翻 ${missing.length} 条${apiKey ? '' : '（无 API key，暂用英文原文）'}`)
  }

  // 按 en.json 的顺序重排，并丢掉源文件里已删除的 key
  const merged = {}
  for (const k of keys) merged[k] = out[k] ?? existing[k]
  writeFileSync(path, JSON.stringify(merged, null, 2) + '\n', 'utf8')
}

if (SOURCE.size !== 2) throw new Error('SOURCE 应恰好含 en 与 zh-CN')
if (failed) process.exit(1)
