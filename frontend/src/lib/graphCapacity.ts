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

// 三档措辞对应三种不同的失败/延迟形态，审查后特别注意两点：
// 1. 不写绝对时长/百分比——LibreOffice 通道的真实耗时由
//    convert_timeout_base_s / per_slide_s / per_mb_s 决定，仓库里没有
//    支撑"一分钟内"这类承诺的实测数据；写死的数字一旦不成立，是在
//    透支这个功能本身的可信度。
// 2. "合并阶段"与"规划阶段"是两码事，不能混用——budget 档失败在合并，
//    reject 档失败在规划，用户看到哪种措辞决定了他对失败时机的预期。
//
// 与 assessGraphRisk 同一份文件：两处上传前/开始转换前的确认横幅
// （App.tsx 里的 pendingFile 与 readyTask 两套独立判定）都要用同一份
// 措辞，不能各写各的、慢慢跑偏。
export const GRAPH_RISK_MESSAGE: Record<Exclude<GraphRisk, 'none'>, string> = {
  shard:
    '此文件较大，Graph 通道会将其切分后分批转换，比不切片更慢。' +
    'LibreOffice 通道不切片，通常明显更快。',
  budget:
    '此文件体积较大，即使 Graph 通道顺利切片、逐片转换成功，仍可能在最终合并阶段因总体积超限而失败——' +
    '这种失败发生在转换即将完成时，最费时间。PDF 实际体积与 pptx 不成固定比例，无法精确预测，但体积越大风险越高。' +
    '建议改用 LibreOffice 引擎。',
  reject:
    '此文件已超过 Graph 通道能处理的分片总容量，大概率会在切片规划阶段就被直接拒绝，不会先切片再浪费转换时间。' +
    '建议改用 LibreOffice 引擎。',
}
