import { describe, expect, it } from 'vitest'
import { assessGraphRisk, GRAPH_RISK_MESSAGE } from './graphCapacity'
import type { CapacityConfig } from './api'

const MIB = 1024 * 1024

// 与后端 config.py 当前默认值一致：12 片 x 40MiB = 480MiB 分片容量，
// 240MiB 合并预算。两个卡点失败的阶段不同——审查 Minor #4 指出这一点：
// >480MiB 在切片规划阶段就被拒（快速失败，不浪费转换时间）；
// (240MiB, 480MiB] 能通过规划、能被 Graph 逐片转完，但合并阶段可能因
// 总字节超限而被拒（最浪费的失败形态，"budget" 档专指这个区间）。
const REALISTIC: CapacityConfig = {
  max_file_size: 600 * MIB,
  graph_max_shards: 12,
  graph_max_shard_bytes: 40 * MIB,
  graph_max_merge_bytes: 240 * MIB,
}

describe('assessGraphRisk', () => {
  it('returns "none" for a small file well under any threshold', () => {
    expect(assessGraphRisk(10 * MIB, REALISTIC)).toBe('none')
  })

  it('returns "none" exactly at the per-shard boundary (not yet over)', () => {
    expect(assessGraphRisk(40 * MIB, REALISTIC)).toBe('none')
  })

  it('returns "shard" just over the per-shard threshold — will be sharded, expect longer wait', () => {
    expect(assessGraphRisk(40 * MIB + 1, REALISTIC)).toBe('shard')
  })

  it('returns "shard" up to and including the merge-budget boundary (240MiB, not yet over)', () => {
    expect(assessGraphRisk(240 * MIB, REALISTIC)).toBe('shard')
  })

  it('returns "budget" just over the merge-budget cliff — worst case: fails only after all shards convert', () => {
    expect(assessGraphRisk(240 * MIB + 1, REALISTIC)).toBe('budget')
  })

  it('returns "budget" up to and including the outer 480MiB shard-capacity ceiling (not yet over)', () => {
    expect(assessGraphRisk(480 * MIB, REALISTIC)).toBe('budget')
  })

  it('returns "reject" just over the 480MiB shard-capacity ceiling — fails fast at planning, not wastefully', () => {
    expect(assessGraphRisk(480 * MIB + 1, REALISTIC)).toBe('reject')
  })

  it('never lets "budget" fire when merge budget is looser than shard capacity', () => {
    // shard capacity = 2 * 10 = 20 bytes; merge budget = 1000 (looser than
    // shard capacity). There is no byte range in this config where the
    // merge budget is the binding constraint — anything over the 20-byte
    // shard capacity must be "reject", never "budget".
    const cfg: CapacityConfig = {
      max_file_size: 100000,
      graph_max_shards: 2,
      graph_max_shard_bytes: 10,
      graph_max_merge_bytes: 1000,
    }
    expect(assessGraphRisk(15, cfg)).toBe('shard') // > 10 (per-shard), <= 20 (shard capacity)
    expect(assessGraphRisk(25, cfg)).toBe('reject') // > 20 (shard capacity) — never "budget"
    expect(assessGraphRisk(2000, cfg)).toBe('reject') // also > merge budget, but still "reject" first
  })

  it('lets "budget" fire before shard capacity when merge budget is the tighter cliff', () => {
    // shard capacity = 3 * 100 = 300; merge budget = 50 (tighter — mirrors
    // REALISTIC above at smaller scale).
    const cfg: CapacityConfig = {
      max_file_size: 100000,
      graph_max_shards: 3,
      graph_max_shard_bytes: 100,
      graph_max_merge_bytes: 50,
    }
    expect(assessGraphRisk(40, cfg)).toBe('none') // <= 50 merge, <= 100 shard bytes
    expect(assessGraphRisk(60, cfg)).toBe('budget') // > 50 merge cliff, <= 300 shard capacity
    expect(assessGraphRisk(400, cfg)).toBe('reject') // > 300 shard capacity
  })
})

describe('GRAPH_RISK_MESSAGE（终审 I-2）', () => {
  // 前端算风险档位用的是剥离前的原始文件体积（选文件/传完那一刻只知道
  // 这个数），但五期起转换前会先统一剥离内嵌视频/音频——真机那份
  // 83.7MB 课件剥离后只剩约 28MB，前端却仍按 83.7MB 判成 'shard' 档、
  // 弹出「比不切片更慢」的提示，用错误的信息劝用户离开 Graph。用户裁决
  // 保留禁用与确认、只改措辞：三档提示都要提一句"实际体积可能明显更低"，
  // 让用户能用准确信息做决定，而不是假装能精确预测。
  it.each(['shard', 'budget', 'reject'] as const)(
    '"%s" 档提到内嵌媒体会被剥离、实际体积可能更低',
    (risk) => {
      expect(GRAPH_RISK_MESSAGE[risk]).toMatch(/剥离/)
      expect(GRAPH_RISK_MESSAGE[risk]).toMatch(/实际.*体积|体积.*实际/)
    },
  )

  it.each(['budget', 'reject'] as const)(
    '"%s" 档：硬性的"建议改用 LibreOffice"必须在"剥离/体积可能更低"这句缓和表述之前，不能紧跟在后面自相矛盾（复审 Minor）',
    (risk) => {
      // 复审指出：「…实际体积可能明显更低，请结合内容自行判断。建议改用
      // LibreOffice 引擎。」——缓和表述与硬推荐紧挨着，语气自相矛盾。
      // 正确顺序是先给结论（建议改用），再补充"不过实际体积可能更低，
      // 可自行判断"这条缓和的但书，而不是反过来。
      const msg = GRAPH_RISK_MESSAGE[risk]
      const adviceIdx = msg.indexOf('建议改用 LibreOffice 引擎')
      const stripIdx = msg.indexOf('剥离')
      expect(adviceIdx).toBeGreaterThan(-1)
      expect(stripIdx).toBeGreaterThan(-1)
      expect(adviceIdx).toBeLessThan(stripIdx)
    },
  )
})
