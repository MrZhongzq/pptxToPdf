from app.services.engines.base import ConversionEngine
from app.services.engines.libreoffice import LibreOfficeEngine
from app.services.engines.placeholder import PlaceholderEngine

_ENGINES: dict[str, type[ConversionEngine]] = {
    PlaceholderEngine.name: PlaceholderEngine,
    LibreOfficeEngine.name: LibreOfficeEngine,
}


def get_engine(name: str) -> ConversionEngine:
    """按名取引擎，每次调用即时实例化。

    _ENGINES 存的是类而不是模块 import 时就构造好的实例：二期
    LibreOfficeEngine.__init__ 若要探测本机 soffice、三期 GraphEngine.__init__
    若要建鉴权客户端，都会在真正被请求时才执行——任一引擎构造失败只影响
    这一次调用，不会在 import 期间就拖垮整个 engines 包，连带 pipeline /
    uploads / main 全部起不来。
    """
    return _ENGINES[name]()


__all__ = ["ConversionEngine", "LibreOfficeEngine", "PlaceholderEngine", "get_engine"]
