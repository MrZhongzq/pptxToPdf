from abc import ABC, abstractmethod
from pathlib import Path

from app.services.pptx_probe import PptxMeta

EMU_PER_POINT = 12700


class ConversionEngine(ABC):
    """转换引擎抽象。不得访问数据库，不得认识 HTTP。"""

    name: str

    @abstractmethod
    def convert(self, src: Path, meta: PptxMeta, dest: Path) -> None:
        """把 src 转成 PDF 写到 dest。失败时抛异常。"""
