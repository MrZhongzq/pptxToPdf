import json
import logging
from datetime import datetime, timezone

from app.config import settings
from app.db import SessionLocal
from app.errors import AppError
from app.models import Task
from app.queue import enqueue_shards
from app.services.engine_router import select_engine
from app.services.engines import get_engine
from app.services.pptx_probe import probe
from app.services.retention import drop_original, purge_expired_outputs, reap_stale_tasks
from app.services.shard_planner import SHARDED_ENGINES, needs_sharding

logger = logging.getLogger(__name__)

BYTES_PER_MB = 1024 * 1024


def compute_timeout_s(slide_count: int, size_bytes: int) -> float:
    """按页数与文件体积算转换超时。

    固定值行不通：10 页与 500 页的合理耗时差一个数量级，
    定小了卡死大文件，定大了让僵死的任务占着 worker 不放。
    只看页数也不够：真实负载里「单节课约 80MB」这类 40 页但内嵌大量
    图片/视频的课件，光按页数算只拿到 max(180, 160)=180 秒，ARM 单核
    解码这些位图很容易超时——所以加一个按体积（MB）的加成项。
    """
    size_mb = size_bytes / BYTES_PER_MB
    return float(
        min(
            max(
                settings.convert_timeout_base_s,
                slide_count * settings.convert_timeout_per_slide_s
                + size_mb * settings.convert_timeout_per_mb_s,
            ),
            settings.convert_timeout_max_s,
        )
    )


def _set_status(session, task: Task, status: str) -> None:
    task.status = status
    session.commit()


def _record_failure(session, task_id: str, code: str, message: str) -> None:
    """失败落库自身也可能失败——回滚后用干净会话重试一次，仍失败则记日志，绝不再抛。"""
    try:
        session.rollback()
        task = session.get(Task, task_id)
        if task is not None:
            task.error_code = code
            task.error_message = message
            task.status = "failed"
            session.commit()
    except Exception:
        logger.exception("无法记录任务 %s 的失败状态", task_id)


def run_task(task_id: str) -> None:
    """走完整状态机：parsing → queued → converting → done / failed。

    这个函数由 RQ worker 在独立子进程里执行，不再是 FastAPI 的
    BackgroundTasks。签名保持只吃 task_id，自开 session。
    """
    session = SessionLocal()
    started = datetime.now(timezone.utc)
    try:
        task = session.get(Task, task_id)
        if task is None:
            logger.warning("run_task 收到不存在的 task_id=%s", task_id)
            return

        src = settings.originals_dir / f"{task_id}.pptx"
        dest = settings.outputs_dir / f"{task_id}.pdf"
        logger.info("task start id=%s file=%s size=%d", task_id, task.original_filename, task.size_bytes)

        try:
            _set_status(session, task, "parsing")
            meta = probe(src)
            size_bytes = src.stat().st_size
            task.slide_count = meta.slide_count
            task.slide_width_emu = meta.slide_width_emu
            task.slide_height_emu = meta.slide_height_emu
            task.fonts_json = json.dumps(list(meta.fonts), ensure_ascii=False)
            task.engine = select_engine(
                meta, size_bytes, requested=task.requested_engine
            )
            logger.info(
                "task parsed id=%s slides=%d engine=%s(requested=%s) options=%s fonts=%s",
                task_id, meta.slide_count, task.engine,
                task.requested_engine or "auto", task.options_json,
                list(meta.fonts)[:20],
            )

            _set_status(session, task, "queued")
            _set_status(session, task, "converting")

            if task.engine in SHARDED_ENGINES and needs_sharding(
                meta.slide_count, size_bytes
            ):
                # 分片路径：本 job 到此为止，终态由汇总 job 落。切不动时
                # prepare_shards 抛 ShardTooLarge / ShardBudgetExceeded，
                # 走下面的 AppError 分支明确失败——绝不静默改用别的引擎。
                # import 放在函数里打断 pipeline ←→ shard_pipeline 的环
                # （shard_pipeline 顶层要用本模块的 compute_timeout_s）。
                from app.services.shard_pipeline import prepare_shards

                shard_ids = prepare_shards(session, task, src, size_bytes)
                enqueue_shards(task_id, shard_ids)
                logger.info(
                    "task sharded id=%s shards=%d，转换与合并交给子 job",
                    task_id, len(shard_ids),
                )
                return

            timeout_s = compute_timeout_s(meta.slide_count, size_bytes)
            get_engine(task.engine).convert(src, meta, dest, timeout_s=timeout_s)

            task.output_path = str(dest.resolve())
            _set_status(session, task, "done")
            logger.info(
                "task done id=%s elapsed=%.1fs output=%d bytes",
                task_id,
                (datetime.now(timezone.utc) - started).total_seconds(),
                dest.stat().st_size,
            )
        except AppError as exc:
            logger.warning("task failed id=%s code=%s msg=%s", task_id, exc.code, exc.message)
            _record_failure(session, task_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001  后台任务兜底，异常静默会让任务永久卡住
            logger.exception("task crashed id=%s", task_id)
            _record_failure(session, task_id, "INTERNAL_ERROR", str(exc))
    finally:
        # 原文件转换结束即删，不论成败——用户要的是 PDF，
        # 失败了他会重传，留着诊断也用不上。这砍掉一半的磁盘增长。
        drop_original(task_id)
        removed = purge_expired_outputs()
        if removed:
            logger.info("retention 清理了 %d 个过期输出", removed)
        # 只在 api 启动时回收孤儿任务不够：worker 容器有内存上限，OOM 是
        # 预期事件，work-horse 被杀后 api 未必会重启，回收器就可能永远不跑。
        # 这里顺带触发一次，与上面 purge_expired_outputs() 同一个惰性模式。
        reap_stale_tasks()
        session.close()
