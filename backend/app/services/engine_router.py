from app.services.pptx_probe import PptxMeta


def select_engine(meta: PptxMeta, size_bytes: int) -> str:
    """按元信息选转换引擎。

    二期只有一个真引擎，恒返回 libreoffice。这个函数存在的意义是
    把选择点固定在 probe 之后——三期 Graph 的判据不只是页数
    （Graph 有 100 页硬上限），还有文件大小（Graph 的转换有 50MB 实测
    上限和 45 秒同步窗口）。`size_bytes` 现在还没用到，先占住签名，
    避免三期改判据时还要回头改 pipeline.py 的调用点和
    conftest.py 里 patch 本函数的 autouse fixture。
    选择逻辑留在 HTTP 层的话，三期得回头改 uploads.py。
    """
    return "libreoffice"
