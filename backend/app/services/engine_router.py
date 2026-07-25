from app.services.pptx_probe import PptxMeta


def select_engine(meta: PptxMeta) -> str:
    """按元信息选转换引擎。

    二期只有一个真引擎，恒返回 libreoffice。这个函数存在的意义是
    把选择点固定在 probe 之后——三期 Graph 的判据是页数
    （Graph 有 100 页硬上限），而页数要解析完才知道。
    选择逻辑留在 HTTP 层的话，三期得回头改 uploads.py。
    """
    return "libreoffice"
