from app.services.engines.base import ConversionEngine
from app.services.engines.placeholder import PlaceholderEngine

_ENGINES: dict[str, ConversionEngine] = {
    PlaceholderEngine.name: PlaceholderEngine(),
}


def get_engine(name: str) -> ConversionEngine:
    """按名取引擎。二期在 _ENGINES 里加一行即可接入 LibreOffice。"""
    return _ENGINES[name]


__all__ = ["ConversionEngine", "PlaceholderEngine", "get_engine"]
