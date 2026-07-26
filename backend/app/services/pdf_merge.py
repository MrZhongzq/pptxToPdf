import logging
import os
from pathlib import Path
from uuid import uuid4

from pypdf import PdfReader, PdfWriter

from app.errors import ConversionFailed

logger = logging.getLogger(__name__)


def merge_pdfs(parts: list[Path], dest: Path) -> int:
    """按 parts 的给定顺序合并，返回总页数。

    顺序由入参决定，不做任何排序——调用方按 TaskShard.index 升序传入。
    在这里加 sorted() 会引入按文件名排序的隐患：分片文件名是 000/001/…，
    但一旦命名规则变化（例如两位数分片数），字典序就会把 shard10.pdf
    排到 shard2.pdf 前面，页序错乱且页数校验完全抓不到，所以严禁在此
    对 parts 做任何形式的重排。
    """
    if not parts:
        raise ConversionFailed("没有可合并的分片")

    writer = PdfWriter()
    for part in parts:
        if not part.is_file():
            raise ConversionFailed(f"分片结果缺失: {part.name}")

        # 每个分片对应原 deck 一段非空闭区间页范围，贡献 0 页在语义上
        # 恒为错误（Graph 对某片返回结构合法但空的 PDF 是真实可能的：
        # 配额截断、渲染失败仍回 200）。这种残缺比页序错乱更隐蔽——
        # 文件能打开、能预览、页序看着连贯，只是缺了中间一段，翻两页
        # 看不出来，只有对照原稿页数才能发现。
        before = len(writer.pages)
        try:
            # pypdf 是惰性解析：PdfReader(...) 构造时只读 xref/trailer，
            # 页对象要到访问 .pages 才真正解析。加密/受保护的分片在构造
            # 阶段完全正常，只有在下面这个迭代里才会炸出
            # FileNotDecryptedError——所以页迭代必须和构造包在同一个
            # try 里，只包住构造行会让这类异常裸着逃出去。
            reader = PdfReader(str(part))
            for page in reader.pages:
                writer.add_page(page)
        except Exception as exc:
            raise ConversionFailed(f"分片 {part.name} 无法解析: {exc}") from exc

        if len(writer.pages) == before:
            raise ConversionFailed(f"分片 {part.name} 是 0 页，合并会产出残缺结果")

    # 写临时文件再原子替换到 dest：
    # 1) 上面的读取循环从不触碰 dest/tmp，所以缺失/损坏/空页分片报错时，
    #    dest 位置原有的文件（例如上一次成功合并的结果）分毫不动——
    #    失败的重试不应该把「有结果」退化成「什么都没有」。
    # 2) 写阶段一旦失败（包括 mkdir/写文件抛出的任何 OSError，例如磁盘满），
    #    只需要清理自己的 tmp；os.replace 是操作系统级原子操作，dest 永远
    #    要么是完整旧版要么是完整新版，不存在半个 PDF 的中间态。
    tmp = dest.parent / f"{dest.stem}.{uuid4().hex}.tmp"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("wb") as fh:
            writer.write(fh)
        os.replace(tmp, dest)
    except Exception as exc:
        # 这里故意用 Exception 而不是 OSError：writer.write() 除了 IO
        # 层的 OSError（磁盘满等），还可能抛 pypdf 自身的
        # PdfWriteError/DependencyError/ValueError 之类非 OSError 异常，
        # 窄了会让 tmp 文件既不清理也不包装，永久留在输出目录里。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            # 善后动作不能顶掉病因：清理失败也要让原始异常照常传出去。
            pass
        raise ConversionFailed(f"合并写入失败: {exc}") from exc

    # 返回值只用来给下游做页数校验；只数内存里 writer.pages 是自我认证，
    # 检不出「写出来的文件和内存里不一致」——回读落盘文件才靠得住。
    # 回读本身也要照 libreoffice.py 的 _verify_output 房规办：失败要
    # 包装成 ConversionFailed，且要清掉这份已确认读不出来的坏 dest，
    # 不能留一份「摆在那儿看着像结果、实际读不出页数」的文件。
    try:
        total = len(PdfReader(str(dest)).pages)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise ConversionFailed(f"合并结果回读校验失败: {exc}") from exc

    logger.info(
        "merged %d 片 -> %d 页 %.1fMB",
        len(parts), total, dest.stat().st_size / 1024 / 1024,
    )
    return total
