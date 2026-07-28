from abc import ABC, abstractmethod
from pathlib import Path

from app.services.pptx_probe import PptxMeta

EMU_PER_POINT = 12700


class ConversionEngine(ABC):
    """转换引擎抽象。不得访问数据库，不得认识 HTTP。"""

    name: str

    @abstractmethod
    def convert(self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float) -> None:
        """把 src 转成 PDF 写到 dest。失败时抛 AppError 子类。

        timeout_s 是单次转换的墙钟上限，实现方必须自己遵守它——
        调外部进程的实现要在超时后杀掉整个进程组，不能只杀父进程。
        """
