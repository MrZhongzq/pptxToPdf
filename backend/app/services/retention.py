import logging
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import SessionLocal
from app.errors import TaskAbandoned
from app.models import Task

logger = logging.getLogger(__name__)

# 非终态状态名——孤儿回收器把卡在这些状态太久的任务标 failed。
NON_TERMINAL = ("pending", "parsing", "queued", "converting")


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


def reap_stale_tasks() -> int:
    """把卡在非终态太久的任务标为失败，返回回收数量。

    RQ 的 job 只活在 worker 进程里。work-horse 被 kill（部署、OOM、崩溃）
    时任务会永远停在中间态，而前端按异步轮询设计，会一直轮询一个
    永不改变的状态。上游设计 §10.3 定的方向是显性要求用户重传。

    只挂在 api 启动时不够：worker 容器有内存上限、要处理大文件，
    OOM 是设计里预期的事件，work-horse 被 OOM killer 干掉后 api 未必
    会重启（restart: unless-stopped），回收器就可能永远不跑。所以
    pipeline.run_task 的 finally 里也会惰性调用本函数，与 api 启动时
    的调用互为兜底。

    时间比较用 naive UTC：SQLAlchemy 的 SQLite dialect 落库时会丢掉
    时区信息，读回来是 naive，拿 aware datetime 去比会 TypeError。
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=settings.stale_task_minutes
    )
    session = SessionLocal()
    try:
        stale = (
            session.query(Task)
            .filter(Task.status.in_(NON_TERMINAL), Task.updated_at < cutoff)
            .all()
        )
        for task in stale:
            task.status = "failed"
            task.error_code = TaskAbandoned.code
            task.error_message = "任务在服务重启前未完成，请重新上传"
        if stale:
            session.commit()
            logger.warning("回收了 %d 个孤儿任务", len(stale))
        return len(stale)
    except Exception:
        logger.exception("回收孤儿任务失败")
        session.rollback()
        return 0
    finally:
        session.close()
