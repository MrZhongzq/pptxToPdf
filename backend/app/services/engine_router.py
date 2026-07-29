import logging

from app.config import settings
from app.services.pptx_probe import PptxMeta

logger = logging.getLogger(__name__)

DEFAULT_ENGINE = "libreoffice"


def select_engine(
    meta: PptxMeta,
    size_bytes: int,
    requested: str | None = None,
    *,
    graph_configured: bool = False,
) -> str:
    """按元信息选转换引擎，用户显式指定时以用户为准。

    自动判定这条路径三期才真正分叉：Graph 有 100 页硬上限、约 50MB 实测
    上限和 45 秒同步窗口，所以判据是页数加大小，而这两个值要 probe 之后
    才知道——这也是选择点必须留在 pipeline 而不是 HTTP 层的原因。

    `requested` 不做白名单校验：未注册的名字会在 `get_engine` 那里抛
    KeyError，由 pipeline 归一成 INTERNAL_ERROR；已注册但未实现的引擎
    （如三期前的 graph）自己抛 EngineUnavailable，错误码更准确。
    在这里再挡一层只会让错误信息离真正的原因更远——**这条只适用于
    requested 分支**，下面的 auto 分支不受这条理由约束。

    `graph_configured` 只影响 auto 分支，绝不允许挪进 requested 分支：
    用户显式选了 graph 而未配置时必须原样返回 "graph"，让后续环节
    （get_engine → load_credentials）报 GraphNotConfigured 明确失败——
    这是项目红线，静默降级到别的引擎是不可接受的。auto 分支不一样：
    "系统自动帮你选"这句话的语义要求选出来的引擎当前必须真的可用，
    三期没有凭证写入路径（管理页面是四期的事），选一个必然打不通的
    graph 没有意义，所以 auto 分支多加一道"Graph 是否已配置"的判据。
    调用方必须自己算好这个布尔值传进来（比如
    `is_graph_configured(session)`）——select_engine 不持有、也不应该
    去开一个 DB 会话，那是刚从 GraphEngine 里搬走的耦合。
    """
    if requested:
        logger.info("engine 由用户指定: %s", requested)
        return requested
    # 自动判定不选切片路径：切片意味着数十次 HTTP 往返与几分钟等待，
    # 作为默认行为太重。用户显式选 Graph 且文件超限时才切片——那是
    # 他知情的选择（见上面 requested 分支，切片判断在 run_task 里）。
    if (
        graph_configured
        and meta.slide_count <= settings.graph_max_pages_per_shard
        and size_bytes <= settings.graph_max_shard_bytes
    ):
        return "graph"
    return DEFAULT_ENGINE
