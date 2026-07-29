from abc import ABC, abstractmethod
from pathlib import Path

from app.services.pptx_probe import PptxMeta

EMU_PER_POINT = 12700


class ConversionEngine(ABC):
    """转换引擎抽象。不得访问数据库——凭证应由上层读好注入，引擎自己开
    DB 会话是已知的设计问题（Graph 引擎曾经违反过这条：Task 8 把
    `SessionLocal()` + `load_credentials()` 移到了 `get_engine()`，
    GraphEngine 现在经构造函数接收已经读好的凭证，见 graph.py 的说明）。

    "不得认识 HTTP" 这条已经不成立：Graph 引擎本质是调远程转换服务，
    必然要说 HTTP，就像 LibreOffice 引擎必然要说 subprocess 一样——
    调用哪种外部机制是具体引擎的实现细节，不是这层抽象要限制的东西。
    """

    name: str

    @abstractmethod
    def convert(self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float) -> None:
        """把 src 转成 PDF 写到 dest。失败时抛 AppError 子类。

        timeout_s 是单次转换的墙钟上限，实现方必须自己遵守它——
        调外部进程的实现要在超时后杀掉整个进程组，不能只杀父进程。
        """
