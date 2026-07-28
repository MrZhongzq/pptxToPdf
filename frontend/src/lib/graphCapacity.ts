import type { CapacityConfig } from './api'

export type GraphRisk = 'none' | 'shard' | 'budget'

/**
 * 上传前的启发式预判，只看文件大小——页数要 probe 之后才知道，PDF 体积
 * 与 pptx 也不成固定比例，无法精确预测。这里给的是诚实的风险分级，
 * 不是假装精确的预测。
 *
 * 两个卡点，取更严的那个：
 * - shard 容量 = graph_max_shards × graph_max_shard_bytes：分片规划阶段的
 *   硬上限，超过这个原始 pptx 大概率在规划阶段就被拒。
 * - merge 预算 = graph_max_merge_bytes：只在所有分片都转换完成后才判——
 *   最浪费的失败形态就是卡在这里：切片规划通过、N 片全部在 Graph 上转完，
 *   最后才吃一个 SHARD_BUDGET_EXCEEDED。这条通常比 shard 容量更严
 *   （240MiB < 480MiB），所以它是预判的主要价值所在。
 *
 * 用 Math.min 现算哪个更严，而不是假设 merge 预算恒定更严——两个数字
 * 都来自后端 settings，将来任一个被调整，这里不需要跟着改判断顺序。
 */
export function assessGraphRisk(fileBytes: number, capacity: CapacityConfig): GraphRisk {
  const shardCapacityBytes = capacity.graph_max_shards * capacity.graph_max_shard_bytes
  const stricterBytes = Math.min(shardCapacityBytes, capacity.graph_max_merge_bytes)

  if (fileBytes > stricterBytes) return 'budget'
  if (fileBytes > capacity.graph_max_shard_bytes) return 'shard'
  return 'none'
}
