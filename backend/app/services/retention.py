import logging
import time

from app.config import settings

logger = logging.getLogger(__name__)


def drop_original(task_id: str) -> None:
    """转换结束后删掉原始 pptx，不论成败。

    用户要的是 PDF；失败了他会重传，留着原文件也没有诊断价值。
    真实文件是 80MB–500MB 量级，不删的话磁盘增长速度翻倍。
    """
    path = settings.originals_dir / f"{task_id}.pptx"
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删除原文件失败 %s: %s", path, exc)


def purge_expired_outputs() -> int:
    """惰性清理过期的输出 PDF，返回删除数量。

    每次任务结束后顺带扫一次，与一期 _purge_expired 同一模式。
    长期没有新任务时不清理，但那也意味着磁盘没在增长，所以惰性成立，
    不需要额外的 cron 容器。
    """
    cutoff = time.time() - settings.output_ttl_hours * 3600
    removed = 0
    try:
        candidates = list(settings.outputs_dir.glob("*.pdf"))
    except OSError as exc:
        logger.warning("扫描输出目录失败: %s", exc)
        return 0

    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            logger.warning("删除过期输出失败 %s: %s", path, exc)
    return removed
