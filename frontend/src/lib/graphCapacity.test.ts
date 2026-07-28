import { describe, expect, it } from 'vitest'
import { assessGraphRisk } from './graphCapacity'
import type { CapacityConfig } from './api'

const MIB = 1024 * 1024

// 与后端 config.py 当前默认值一致：12 片 x 40MiB = 480MiB 分片容量，
// 240MiB 合并预算——后者更严，也是最浪费的失败形态（12 片全部转完
// 才在合并阶段被拒）真正卡的地方。
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

  it('returns "shard" up to and including the stricter cliff (240MiB)', () => {
    expect(assessGraphRisk(240 * MIB, REALISTIC)).toBe('shard')
  })

  it('returns "budget" just over the stricter cliff — worst-case: fails only after all shards convert', () => {
    expect(assessGraphRisk(240 * MIB + 1, REALISTIC)).toBe('budget')
  })

  it('returns "budget" for a file at the outer 480MiB shard-capacity ceiling', () => {
    expect(assessGraphRisk(480 * MIB, REALISTIC)).toBe('budget')
  })

  it('picks the stricter cliff dynamically — merge budget looser than shard capacity', () => {
    // shard capacity = 2 * 10 = 20 bytes; merge budget = 1000 (looser).
    // stricter cliff must be the 20-byte shard capacity, not a hardcoded
    // "merge budget is always stricter" assumption.
    const cfg: CapacityConfig = {
      max_file_size: 100000,
      graph_max_shards: 2,
      graph_max_shard_bytes: 10,
      graph_max_merge_bytes: 1000,
    }
    expect(assessGraphRisk(15, cfg)).toBe('shard') // > 10 (per-shard), <= 20 (stricter cliff)
    expect(assessGraphRisk(25, cfg)).toBe('budget') // > 20 (stricter cliff = shard capacity here)
  })

  it('picks the stricter cliff dynamically — merge budget tighter than shard capacity', () => {
    // shard capacity = 3 * 100 = 300; merge budget = 50 (tighter — the realistic case).
    const cfg: CapacityConfig = {
      max_file_size: 100000,
      graph_max_shards: 3,
      graph_max_shard_bytes: 100,
      graph_max_merge_bytes: 50,
    }
    // Stricter cliff (50) is below the per-shard threshold (100), so
    // crossing the per-shard threshold at all already means crossing the
    // stricter cliff — "shard" tier collapses, straight to "budget".
    expect(assessGraphRisk(60, cfg)).toBe('budget')
    expect(assessGraphRisk(40, cfg)).toBe('none')
  })
})
