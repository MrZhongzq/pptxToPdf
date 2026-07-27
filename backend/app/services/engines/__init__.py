from sqlalchemy.orm import Session

from app.services.engines.base import ConversionEngine
from app.services.engines.graph import GraphEngine
from app.services.engines.libreoffice import LibreOfficeEngine
from app.services.engines.placeholder import PlaceholderEngine
from app.services.graph_credentials import load_credentials

_ENGINES: dict[str, type[ConversionEngine]] = {
    PlaceholderEngine.name: PlaceholderEngine,
    LibreOfficeEngine.name: LibreOfficeEngine,
    GraphEngine.name: GraphEngine,
}


def get_engine(name: str, *, session: Session | None = None) -> ConversionEngine:
    """按名取引擎，每次调用即时实例化。

    _ENGINES 存的是类而不是模块 import 时就构造好的实例：二期
    LibreOfficeEngine.__init__ 若要探测本机 soffice、三期 GraphEngine.__init__
    若要建鉴权客户端，都会在真正被请求时才执行——任一引擎构造失败只影响
    这一次调用，不会在 import 期间就拖垮整个 engines 包，连带 pipeline /
    uploads / main 全部起不来。

    Graph 引擎的凭证在这里读好再注入构造函数（Task 8 从 GraphEngine.convert()
    移出来的设计改动）：ConversionEngine 子类不得访问数据库，读凭证这件事
    只能发生在构造引擎实例的这一层，而不是引擎自己的方法里。`session` 是
    调用方（pipeline.run_task / shard_pipeline.convert_shard）已经开着的
    会话，复用它而不是自己再开一条连接。非 graph 引擎不碰 session，
    传不传都无所谓。
    """
    cls = _ENGINES[name]
    if issubclass(cls, GraphEngine):
        if session is None:
            raise ValueError(
                "get_engine('graph', ...) 需要传入 session 才能加载凭证；"
                "调用方必须持有一个活跃的数据库会话"
            )
        return GraphEngine(load_credentials(session))
    return cls()


def available_engines() -> list[str]:
    """已注册的引擎名。注册不等于可用——graph 三期起是真实实现，但没配置
    Azure 凭证时会抛 GraphNotConfigured（不是 EngineUnavailable），
    见 graph.py 和 graph_credentials.py 的说明。"""
    return sorted(_ENGINES)


__all__ = [
    "ConversionEngine",
    "GraphEngine",
    "LibreOfficeEngine",
    "PlaceholderEngine",
    "available_engines",
    "get_engine",
]
