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

    # 写临时文件、回读校验、再原子替换到 dest——三步都不碰 dest 本身，
    # 直到最后一步的 os.replace：
    # 1) 上面的读取循环、下面的写入和回读校验全部只动 tmp，所以缺失/
    #    损坏/空页分片报错、写失败、回读失败，dest 位置原有的文件
    #    （例如上一次成功合并的结果）分毫不动——失败的重试不应该把
    #    「有结果」退化成「什么都没有」。
    # 2) os.replace 是操作系统级原子操作，一旦执行到它，dest 永远要么
    #    是完整旧版要么是完整新版，不存在半个 PDF 的中间态。
    tmp = dest.parent / f"{dest.stem}.{uuid4().hex}.tmp"

    def _cleanup_tmp() -> None:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            # 善后动作不能顶掉病因：清理失败也要让原始异常照常传出去。
            pass

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("wb") as fh:
            writer.write(fh)
    except Exception as exc:
        # 这里故意用 Exception 而不是 OSError：writer.write() 除了 IO
        # 层的 OSError（磁盘满等），还可能抛 pypdf 自身的
        # PdfWriteError/DependencyError/ValueError 之类非 OSError 异常，
        # 窄了会让 tmp 文件既不清理也不包装，永久留在输出目录里。
        _cleanup_tmp()
        raise ConversionFailed(f"合并写入失败: {exc}") from exc

    # 回读校验必须放在 os.replace 之前、对 tmp（不是 dest）回读：
    # write() 成功返回不代表内容合法——这正是这项校验要抓的失败模式，
    # "写成功但内容非法"。只数内存里 writer.pages 是自我认证，检不出
    # 这种落盘内容与内存不一致的情况。如果先 replace 再回读，一旦回读
    # 失败，dest 上原本那份完整的旧结果已经被替换掉、回不去了，只能在
    # 「留一份坏文件」和「连旧版本一起删掉」两个更差的选项里选一个。
    # 校验放在 replace 之前，dest 在验证通过之前从头到尾不会被触碰，
    # 旧版本天然保住，不用做这个取舍。
    # （libreoffice.py 的 _verify_output 把校验放在写完之后，是因为
    # soffice 直接写 dest、没有 tmp+replace 这层缓冲，它没得选；这里
    # 前提不同——有 tmp 可用，就不该机械照搬那个顺序。）
    try:
        total = len(PdfReader(str(tmp)).pages)
    except Exception as exc:
        _cleanup_tmp()
        raise ConversionFailed(f"合并结果回读校验失败: {exc}") from exc

    try:
        os.replace(tmp, dest)
    except Exception as exc:
        _cleanup_tmp()
        raise ConversionFailed(f"合并写入失败: {exc}") from exc

    logger.info(
        "merged %d 片 -> %d 页 %.1fMB",
        len(parts), total, dest.stat().st_size / 1024 / 1024,
    )
    return total
