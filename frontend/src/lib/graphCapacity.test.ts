import { describe, expect, it } from 'vitest'
import { assessGraphRisk } from './graphCapacity'
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
