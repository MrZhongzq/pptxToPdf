import logging

from app.config import settings
from app.services.pptx_probe import PptxMeta

logger = logging.getLogger(__name__)

DEFAULT_ENGINE = "libreoffice"


def select_engine(
    meta: PptxMeta, size_bytes: int, requested: str | None = None
) -> str:
    """按元信息选转换引擎，用户显式指定时以用户为准。

    自动判定这条路径三期才真正分叉：Graph 有 100 页硬上限、约 50MB 实测
    上限和 45 秒同步窗口，所以判据是页数加大小，而这两个值要 probe 之后
    才知道——这也是选择点必须留在 pipeline 而不是 HTTP 层的原因。

    `requested` 不做白名单校验：未注册的名字会在 `get_engine` 那里抛
    KeyError，由 pipeline 归一成 INTERNAL_ERROR；已注册但未实现的引擎
    （如三期前的 graph）自己抛 EngineUnavailable，错误码更准确。
    在这里再挡一层只会让错误信息离真正的原因更远。
    """
    if requested:
        logger.info("engine 由用户指定: %s", requested)
        return requested
    # 自动判定不选切片路径：切片意味着数十次 HTTP 往返与几分钟等待，
    # 作为默认行为太重。用户显式选 Graph 且文件超限时才切片——那是
    # 他知情的选择（见上面 requested 分支，切片判断在 run_task 里）。
    if (
        meta.slide_count <= settings.graph_max_pages_per_shard
        and size_bytes <= settings.graph_max_shard_bytes
    ):
        return "graph"
    return DEFAULT_ENGINE
