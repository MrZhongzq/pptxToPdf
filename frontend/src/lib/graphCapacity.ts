import type { CapacityConfig } from './api'

export type GraphRisk = 'none' | 'shard' | 'budget' | 'reject'

/**
 * 上传前的启发式预判，只看文件大小——页数要 probe 之后才知道，PDF 体积
 * 与 pptx 也不成固定比例，无法精确预测。这里给的是诚实的风险分级，
 * 不是假装精确的预测。
 *
 * 三档对应三种不同的失败/延迟形态，审查 Minor #4 之后按"失败发生在哪个
 * 阶段"拆开，不再用一个笼统的"更严卡点"含糊带过：
 *
 * - 'shard'：原始 pptx 超过单片体积上限（graph_max_shard_bytes），会被
 *   切成多片分批转换——纯粹是"会更慢"，不是失败风险。
 * - 'budget'：超过 merge 预算（graph_max_merge_bytes）但仍在分片总容量
 *   （graph_max_shards × graph_max_shard_bytes）以内——能通过切片规划、
 *   能被 Graph 逐片转完，但合并阶段可能因转换后总字节超限而被拒。这是
 *   最浪费的失败形态：失败发生在转换即将完成时。
 * - 'reject'：超过分片总容量本身——大概率在切片规划阶段就被直接拒绝
 *   （SHARD_TOO_LARGE 或规划期的 422），根本走不到转换和合并，是快速
 *   失败而不是最浪费的那种。
 *
 * 判定顺序天然处理了"哪个卡点更严"因配置而变的情况：先判分片总容量，
 * 后判 merge 预算——如果 merge 预算比分片总容量更松（罕见配置），
 * 任何超过分片总容量的文件在到达 merge 预算判断之前就已经被
 * 'reject' 接住，'budget' 不会误触发。
 */
export function assessGraphRisk(fileBytes: number, capacity: CapacityConfig): GraphRisk {
  const shardCapacityBytes = capacity.graph_max_shards * capacity.graph_max_shard_bytes

  if (fileBytes > shardCapacityBytes) return 'reject'
  if (fileBytes > capacity.graph_max_merge_bytes) return 'budget'
  if (fileBytes > capacity.graph_max_shard_bytes) return 'shard'
  return 'none'
}
