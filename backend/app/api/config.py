from fastapi import APIRouter

from app.config import settings
from app.schemas import CapacityConfig

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/capacity", response_model=CapacityConfig)
def get_capacity() -> CapacityConfig:
    """只读，只吐容量相关的数字，供前端在选 Graph 引擎时做上传前的启发式
    预判——避免用户白传几百 MB 才在分片规划阶段吃 422，见
    app.config.settings.graph_max_shards 的文档字符串。

    值现读 settings 单例（不是端点里抄一份常量），四期上真实租户后回调
    settings 里的倍率/上限时，本端点自动跟着变，不需要同步改这里。
    绝不返回任何凭证/配置状态（如 Graph 是否已配置）——那是四期管理页的
    范畴，也是信息泄露面。
    """
    return CapacityConfig(
        max_file_size=settings.max_file_size,
        graph_max_shards=settings.graph_max_shards,
        graph_max_shard_bytes=settings.graph_max_shard_bytes,
        graph_max_merge_bytes=settings.graph_max_merge_bytes,
    )
