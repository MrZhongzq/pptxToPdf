import logging
from pathlib import Path

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
    try:
        for part in parts:
            if not part.is_file():
                raise ConversionFailed(f"分片结果缺失: {part.name}")
            try:
                reader = PdfReader(str(part))
                for page in reader.pages:
                    writer.add_page(page)
            except ConversionFailed:
                raise
            except Exception as exc:
                raise ConversionFailed(
                    f"分片 {part.name} 无法解析: {exc}"
                ) from exc

        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            writer.write(fh)
    except Exception:
        # 半个合并结果比没有更糟：后续的页数校验会拿它当有效产物。
        dest.unlink(missing_ok=True)
        raise

    total = len(writer.pages)
    logger.info(
        "merged %d 片 -> %d 页 %.1fMB",
        len(parts), total, dest.stat().st_size / 1024 / 1024,
    )
    return total
