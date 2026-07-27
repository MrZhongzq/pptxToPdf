import logging

from app.config import settings

logger = logging.getLogger(__name__)

SHARDED_ENGINES: frozenset[str] = frozenset({"graph"})
"""需要切片的引擎。下面这两个阈值是 Graph 的硬限制（100 页上限、45 秒同步
窗口），LibreOffice 在本地跑没有这类约束，拿这套阈值去切它只会平白多出
切片/合并两道出错面。"""


def needs_sharding(slide_count: int, size_bytes: int) -> bool:
    """超过任一维度就需要切片。"""
    return (
        slide_count > settings.graph_max_pages_per_shard
        or size_bytes > settings.graph_max_shard_bytes
    )


def plan_ranges(
    slide_count: int, size_bytes: int, max_pages: int, max_bytes: int
) -> list[tuple[int, int]]:
    """算出 1-based 闭区间的页范围列表，无缝无重叠覆盖整个 deck。

    体积维度按每页均摊估算，这只是一个初分——不是终局判定。真实 deck 的
    体积分布经常极端失真（某一页嵌了 100MB 视频、其余各页几 KB 是常见形
    状），均摊估算在任一方向上都可能出错：可能提前把明明切得开的 deck 判
    死，也可能放行一个实际切不动的分片。本函数不承担、也没有能力承担「是
    否真的能塞进 max_bytes」这个判断——它手上只有总大小和页数，没有逐页
    的真实体积。

    `max(1, ...)` 的 clamp 表达的是切分粒度的下限：一页不能再切成两半，
    所以估算再怎么超标，单片也不会低于 1 页。这不是「尽力而为」的妥协，
    是切分单位本身的物理约束。

    调用方（shard_pipeline）切完之后必须用 os.path.getsize() 实测每片的
    真实体积：仍在预算内就直接用；超了就在那一片上再切一轮；如果一片已经
    只剩 1 页却仍然超过 max_bytes（页面本身不可再分），调用方要显式抛
    ShardTooLarge，不能静默放行或悄悄换引擎。这是本函数与 shard_pipeline
    之间的契约——终局判定权在实测这一侧，不在这里。
    """
    if slide_count <= 0:
        raise ValueError(f"slide_count 必须为正，收到 {slide_count}")

    avg_page_bytes = max(1, size_bytes // slide_count)
    by_size = max(1, max_bytes // avg_page_bytes)
    pages_per_shard = max(1, min(max_pages, by_size))

    ranges = []
    start = 1
    while start <= slide_count:
        end = min(start + pages_per_shard - 1, slide_count)
        ranges.append((start, end))
        start = end + 1

    logger.info(
        "分片规划: %d 页 %.1fMB -> %d 片，每片最多 %d 页",
        slide_count, size_bytes / 1024 / 1024, len(ranges), pages_per_shard,
    )
    return ranges
