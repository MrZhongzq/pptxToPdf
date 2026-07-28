import logging
import shutil
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import SessionLocal
from app.errors import TaskAbandoned
from app.models import Task, TaskShard

logger = logging.getLogger(__name__)

# 非终态状态名——孤儿回收器把卡在这些状态太久的任务标 failed。
# "merging" 是三期分片路径的中间态，必须在列：merge job 被部署/OOM/kill
# 打断后任务会永远停在这个状态，不在列的话回收器捞不到，前端只能轮询到
# 超时，用户拿到的是一句"轮询超时"而不是明确的失败原因。
NON_TERMINAL = ("pending", "parsing", "queued", "converting", "merging")


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


def purge_expired_shards() -> int:
    """清理过期的分片目录，返回删除数量。

    正常路径下 merge_shards 的 finally 会删掉自己的分片目录，discard_shards
    处理入队失败的撤销路径。这个函数收拾的是两者都够不到的残骸：worker 在
    convert_shard / merge_shards 中途被 OOM killer 干掉时，没有任何 finally
    会跑，分片目录会留下几十 MB 到几百 MB（分片 pptx 加分片 PDF 是原文件的
    两倍体积），没有任何其他路径会碰它。
    """
    cutoff = time.time() - settings.output_ttl_hours * 3600
    removed = 0
    try:
        candidates = list(settings.shards_dir.iterdir())
    except OSError:
        return 0

    for path in candidates:
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError as exc:
            logger.warning("删除过期分片目录失败 %s: %s", path, exc)
    return removed


def _is_really_stale(session, task: Task, cutoff: datetime) -> bool:
    """Task.updated_at 说它超时了，再问一次分片：它真的没在动吗？

    二期的判据靠一个不变量成立：单次转换的墙钟被 convert_timeout_max_s
    (默认 1800s = 30 分钟) 封顶，小于 stale_task_minutes (45 分钟)，所以
    "Task 行 45 分钟没动" 必然意味着 worker 死了。

    三期的分片路径打破了这个不变量，而且是从两头打破的：
    1. 总墙钟变成 N 片之和，12 × 1800s = 6 小时，远超 45 分钟；
    2. Task 行从 prepare_shards 那次 commit 之后就再也不被写——子 job 只写
       自己那行 TaskShard（这是刻意的：并发自增同一行在 SQLite 上要么加锁
       要么丢更新，不能改），于是 Task.updated_at 干脆不再是活性信号。

    两者叠加的后果不只是误杀：任务被标 failed 之后分片仍会陆续跑完，
    merge_shards 读的是 TaskShard.status、不看 Task.status，会把终态从
    failed 一路改回 done——而前端见 failed 早已停止轮询，原文件也已被
    drop_original 删掉。所以这里改看该任务最新的 TaskShard.updated_at。

    只有一片都没动过 45 分钟才判定 worker 真的死了。没有任何分片行
    （shard_total 已落但建行失败之类）时退回原判据，不特殊放行。
    """
    if task.shard_total is None:
        return True  # 二期原路径，判据一个字不改
    last = (
        session.query(TaskShard.updated_at)
        .filter(TaskShard.task_id == task.task_id)
        .order_by(TaskShard.updated_at.desc())
        .limit(1)
        .scalar()
    )
    if last is None:
        return True
    return last < cutoff


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
        candidates = (
            session.query(Task)
            .filter(Task.status.in_(NON_TERMINAL), Task.updated_at < cutoff)
            .all()
        )
        stale = [t for t in candidates if _is_really_stale(session, t, cutoff)]
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
