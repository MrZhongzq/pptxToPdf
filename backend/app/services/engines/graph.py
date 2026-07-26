from pathlib import Path

from app.errors import EngineUnavailable
from app.services.engines.base import ConversionEngine
from app.services.pptx_probe import PptxMeta


class GraphEngine(ConversionEngine):
    """Microsoft Graph 引擎的占位实现，三期填充。

    现在就注册它，是为了让前端的引擎选择能拿到一个**语义准确的错误**：
    未注册的引擎名会在 get_engine 处抛 KeyError，被 pipeline 归一成
    INTERNAL_ERROR，用户只能看到一句无意义的 Python 异常字符串；
    注册成桩之后失败落的是 ENGINE_UNAVAILABLE，前端能照常展示原因。

    三期实现时的已知约束（见设计文档 §9）：Graph 的 format=pdf 有约
    100 页硬上限、45 秒同步超时、约 50MB 实测大小上限，超出的 deck
    需要先切片再合并，所以它不是 LibreOffice 的替代品而是小文件的
    高保真增强。
    """

    name = "graph"

    def convert(
        self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float
    ) -> None:
        raise EngineUnavailable(
            "Microsoft Graph 引擎尚未实现（计划在三期）。"
            "请改用 LibreOffice 引擎重新上传。"
        )
