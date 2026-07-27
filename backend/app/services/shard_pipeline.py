"""分片流水线：规划页范围 → 切片 → 每片独立转换 → 合并回一份 PDF。

三个阶段跑在三种进程里，彼此不共享内存状态，一切协调都过数据库：

- `prepare_shards` 由 `pipeline.run_task` 在它自己的 worker 进程里调用，
  负责把 deck 切开并落 TaskShard 行；
- `convert_shard` 每片一个独立 RQ job（各自 fork 一个子进程，这是选 RQ 而非
  ARQ 的原因：转换是阻塞式 subprocess，需要崩溃隔离），只写自己那一行；
- `merge_shards` 是依赖上面全部分片 job 的汇总 job，读所有 TaskShard 的状态
  自行判定成败。

Task 上刻意不存"已完成分片计数"：多个分片并发完成时自增同一行在 SQLite 上
要么加锁要么丢更新，而 `TaskShard.status` 本来就是这件事的唯一真相源。
"""
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.errors import (
    AppError,
    ConversionFailed,
    ConversionPageMismatch,
    ShardBudgetExceeded,
    ShardTooLarge,
)
from app.models import Task, TaskShard
from app.services.engines import get_engine
from app.services.pdf_merge import merge_pdfs
from app.services.pipeline import compute_timeout_s
from app.services.pptx_probe import probe
from app.services.pptx_split import split_pptx
from app.services.shard_planner import plan_ranges

logger = logging.getLogger(__name__)

MIB = 1024 * 1024


def shard_dir(task_id: str) -> Path:
    return settings.shards_dir / task_id


# ---------------------------------------------------------------- 规划与切片


def _enforce_shard_count(count: int, stage: str) -> None:
    """分片数的显式上限。

    两个调用点都必要，不是重复：规划输出那次是快速失败（还没开始切，省掉
    整轮 IO），实测复核之后那次才是权威——重切逻辑会让分片数比规划值更多，
    只卡规划值等于没卡。
    """
    if count > settings.graph_max_shards:
        raise ShardBudgetExceeded(
            f"{stage}需要 {count} 个分片，超过上限 {settings.graph_max_shards}。"
            f"合并阶段会把所有分片一次性载入内存，请拆分文件后分次转换"
        )


def _split_within_budget(
    src: Path, ranges: list[tuple[int, int]], work_root: Path, max_bytes: int
) -> list[tuple[tuple[int, int], Path]]:
    """切片并用实测体积复核，超预算的范围再切一轮。返回 [(页范围, 分片路径)]。

    这是 `plan_ranges` 显式留给调用方的契约。规划器手上只有「总体积 ÷ 页数」
    的均值，而真实 deck 的体积分布经常极端失真（一页嵌 100MB 视频、其余各
    1KB 是常见形状），而且共享的 theme/master 会在每个分片里各留一份，所以
    均摊估算在任一方向上都可能出错。不实测复核的话，规划器基于失真估算放行
    的分片会直接怼给 Graph，换回一个难懂的 Graph 错误。

    收敛性：每一轮里，超预算的范围被对半拆成两段，两段的页数都严格小于原
    范围（因为只在 start < end 时才拆）；页数减到 1 还超预算时没有再切的
    余地，直接抛 ShardTooLarge。所以轮数上界是 log2(最大范围页数)+1，
    不存在无限重切。

    每一轮切进各自的 `_pass{n}` 子目录：`split_pptx` 固定按 `{idx:03d}.pptx`
    命名，切回同一个目录会覆盖上一轮已经通过复核的分片。
    """
    pending = list(ranges)
    accepted: list[tuple[tuple[int, int], Path]] = []
    round_no = 0

    while pending:
        parts = split_pptx(src, pending, work_root / f"_pass{round_no}")
        retry: list[tuple[int, int]] = []
        for (start, end), part in zip(pending, parts):
            # 实测，不用估算——这一行就是本函数存在的全部理由
            actual = os.path.getsize(part)
            if actual <= max_bytes:
                accepted.append(((start, end), part))
                continue
            if start == end:
                raise ShardTooLarge(
                    f"第 {start} 页单页就有 {actual / MIB:.1f}MB，超过分片上限 "
                    f"{max_bytes / MIB:.1f}MB。单页无法再切分，请精简该页的"
                    f"内嵌媒体后重试"
                )
            mid = start + (end - start) // 2
            retry.extend([(start, mid), (mid + 1, end)])
            part.unlink(missing_ok=True)  # 这一片作废，不占磁盘也不进最终结果
            logger.info(
                "分片 %d-%d 实测 %.1fMB 超预算，拆成 %d-%d / %d-%d 再切",
                start, end, actual / MIB, start, mid, mid + 1, end,
            )
        pending = retry
        round_no += 1

    # 页序即合并序：accepted 跨轮累积，顺序已被打乱，必须按 page_start 排回来
    accepted.sort(key=lambda item: item[0][0])
    return accepted


def prepare_shards(
    session: Session, task: Task, src: Path, size_bytes: int
) -> list[str]:
    """规划、切片、实测复核，落 TaskShard 行。返回按 index 升序的 shard_id。

    复用调用方的 session 而不是自开一个：run_task 的会话此刻可能持有写事务，
    同一进程内再开一条 SQLite 连接去写同一个库，等的是自己的锁。
    """
    slide_count = task.slide_count
    if not slide_count:
        raise ConversionFailed("分片前必须先 probe 出页数")

    ranges = plan_ranges(
        slide_count,
        size_bytes,
        settings.graph_max_pages_per_shard,
        settings.graph_max_shard_bytes,
    )
    _enforce_shard_count(len(ranges), "规划")

    work = shard_dir(task.task_id)
    # 上一次失败留下的残骸会让编号错位，先清干净再开工
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    try:
        accepted = _split_within_budget(
            src, ranges, work, settings.graph_max_shard_bytes
        )
        _enforce_shard_count(len(accepted), "实测复核后")

        final: list[tuple[int, int]] = []
        for idx, ((start, end), part) in enumerate(accepted):
            # 搬到 convert_shard 约定的位置：分片 job 只拿到 shard_id，
            # 靠 index 推算路径，不再回查文件系统。
            os.replace(part, work / f"{idx:03d}.pptx")
            final.append((start, end))
        for child in list(work.iterdir()):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    except Exception:
        # 失败路径也要清中间产物：一次 500MB 的转换能留下 1GB 残骸。
        # ignore_errors=True 保证清理失败不会顶掉原始异常。
        shutil.rmtree(work, ignore_errors=True)
        raise

    shard_ids: list[str] = []
    for idx, (start, end) in enumerate(final):
        shard_id = uuid4().hex
        session.add(
            TaskShard(
                shard_id=shard_id,
                task_id=task.task_id,
                index=idx,
                page_start=start,
                page_end=end,
                status="pending",
            )
        )
        shard_ids.append(shard_id)
    task.shard_total = len(shard_ids)
    session.commit()

    logger.info(
        "task %s 切成 %d 片（规划 %d 片），页范围 %s",
        task.task_id, len(final), len(ranges), final,
    )
    return shard_ids


# ---------------------------------------------------------------- 分片转换


def _record_shard_failure(
    session: Session, shard_id: str, code: str, message: str
) -> None:
    """与 pipeline._record_failure 同款自保护：落库本身失败时回滚重取，
    仍失败只记日志，绝不再抛——否则 RQ 会把它当作 job 崩溃，而汇总 job
    看到的分片状态还停在 converting。"""
    try:
        session.rollback()
        shard = session.get(TaskShard, shard_id)
        if shard is not None:
            shard.status = "failed"
            shard.error_code = code
            shard.error_message = message
            session.commit()
    except Exception:
        logger.exception("无法记录分片 %s 的失败状态", shard_id)


def convert_shard(shard_id: str) -> None:
    """转换单个分片。只写自己那行 TaskShard，不碰主任务状态——
    多个分片并发运行时不能有对同一行的写竞争。"""
    session = SessionLocal()
    try:
        shard = session.get(TaskShard, shard_id)
        if shard is None:
            logger.warning("convert_shard 收到不存在的 shard_id=%s", shard_id)
            return
        task = session.get(Task, shard.task_id)
        if task is None:
            logger.warning("分片 %s 的主任务已不存在", shard_id)
            return

        src = shard_dir(shard.task_id) / f"{shard.index:03d}.pptx"
        dest = shard_dir(shard.task_id) / f"{shard.index:03d}.pdf"

        shard.status = "converting"
        session.commit()

        try:
            if not src.is_file():
                raise ConversionFailed(f"分片源文件缺失: {src.name}")
            meta = probe(src)
            # 按这一片自己的页数与体积算预算，而不是套一个全局常量：
            # graph_request_timeout_s 是单个 HTTP 请求的超时，拿它当整片的
            # 墙钟预算会让 40MB 分片的上传还没传完就判超时，而且引擎内部的
            # 退避重试会因为预算不足而一次都不重试。
            timeout_s = compute_timeout_s(meta.slide_count, src.stat().st_size)
            # 用 Task.engine，不做任何回退——用户显式选了 Graph 而条件不满足
            # 时必须明确报错，绝不能偷偷改用别的引擎。
            get_engine(task.engine).convert(src, meta, dest, timeout_s=timeout_s)
            shard.output_path = str(dest.resolve())
            shard.status = "done"
            session.commit()
            logger.info(
                "shard done id=%s 页 %d-%d", shard_id, shard.page_start, shard.page_end
            )
        except AppError as exc:
            logger.warning("shard failed id=%s code=%s", shard_id, exc.code)
            _record_shard_failure(session, shard_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001  后台任务兜底
            logger.exception("shard crashed id=%s", shard_id)
            _record_shard_failure(session, shard_id, "INTERNAL_ERROR", str(exc))
    finally:
        session.close()


# ---------------------------------------------------------------- 汇总合并


def _fail_task(session: Session, task_id: str, code: str, message: str) -> None:
    try:
        session.rollback()
        task = session.get(Task, task_id)
        if task is not None:
            task.status = "failed"
            task.error_code = code
            task.error_message = message
            session.commit()
    except Exception:
        logger.exception("无法记录任务 %s 的失败状态", task_id)


def _collect_parts(shards: list[TaskShard]) -> list[Path]:
    """按 index 升序取出各分片 PDF 路径，并核对合并输入的总字节预算。

    顺序契约在这里落实：`merge_pdfs` 明确不排序，页序完全由这个列表决定。
    """
    parts: list[Path] = []
    total = 0
    for shard in shards:
        if not shard.output_path:
            raise ConversionFailed(f"分片 {shard.index} 标记为成功但没有输出路径")
        path = Path(shard.output_path)
        if not path.is_file():
            raise ConversionFailed(f"分片 {shard.index} 的输出文件已丢失: {path.name}")
        total += os.path.getsize(path)
        parts.append(path)

    # 必须在 merge_pdfs 之前判：那一步会把所有分片一次性载入同一个 PdfWriter，
    # 峰值约 2.2 倍输入体积。判晚了就不是报错而是 OOM，任务永远卡在 merging。
    if total > settings.graph_max_merge_bytes:
        raise ShardBudgetExceeded(
            f"待合并的分片共 {total / MIB:.1f}MB，超过合并上限 "
            f"{settings.graph_max_merge_bytes / MIB:.1f}MB。"
            f"合并需要约 2 倍于此的内存，请拆分文件后分次转换"
        )
    return parts


def merge_shards(task_id: str) -> None:
    """汇总：检查所有分片，全成功则按序合并，任一失败则整体失败。

    由 RQ 的 Dependency(allow_failure=True) 触发——默认的 allow_failure=False
    会让任一分片失败时本 job 永远停在 DeferredJobRegistry 里，任务卡死在
    converting，只能等孤儿回收器 45 分钟后收尸。

    不做"把成功的几片拼起来当完整结果交付"：一份缺了中间几页的 PDF 能打开、
    能预览、页序看着连贯，翻两页看不出问题，比明确报错糟糕得多。
    """
    session = SessionLocal()
    started = datetime.now(timezone.utc)
    try:
        task = session.get(Task, task_id)
        if task is None:
            logger.warning("merge_shards 收到不存在的 task_id=%s", task_id)
            return

        shards = (
            session.query(TaskShard)
            .filter(TaskShard.task_id == task_id)
            .order_by(TaskShard.index)
            .all()
        )
        failed = [s for s in shards if s.status == "failed"]
        unfinished = [s for s in shards if s.status not in ("done", "failed")]

        if not shards or failed or unfinished:
            first = failed[0] if failed else None
            code = (first.error_code if first else None) or "CONVERSION_FAILED"
            if first is not None:
                msg = (
                    f"分片 {first.index}（页 {first.page_start}-{first.page_end}）"
                    f"失败：{first.error_message}"
                )
            elif unfinished:
                msg = f"{len(unfinished)} 个分片未完成"
            else:
                msg = "任务标记为分片转换但没有任何分片记录"
            logger.warning("task failed id=%s %s", task_id, msg)
            _fail_task(session, task_id, code, msg)
            return

        task.status = "merging"
        session.commit()

        try:
            dest = settings.outputs_dir / f"{task_id}.pdf"
            pages = merge_pdfs(_collect_parts(shards), dest)

            if task.slide_count is not None and pages != task.slide_count:
                dest.unlink(missing_ok=True)
                raise ConversionPageMismatch(
                    f"合并后页数不符：期望 {task.slide_count} 页，实际 {pages} 页"
                )

            task.output_path = str(dest.resolve())
            task.status = "done"
            session.commit()
            logger.info(
                "task done id=%s shards=%d pages=%d elapsed=%.1fs",
                task_id, len(shards), pages,
                (datetime.now(timezone.utc) - started).total_seconds(),
            )
        except AppError as exc:
            logger.warning("merge failed id=%s code=%s", task_id, exc.code)
            _fail_task(session, task_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("merge crashed id=%s", task_id)
            _fail_task(session, task_id, "INTERNAL_ERROR", str(exc))
    finally:
        # 中间产物体积是原文件的两倍（分片 pptx + 分片 PDF），不论成败都要清，
        # 否则一次 500MB 的转换会留下 1GB 残骸。ignore_errors=True：清理失败
        # 不许淹没原始错误，任务状态已经落库，残骸是运维问题。
        shutil.rmtree(shard_dir(task_id), ignore_errors=True)
        session.close()
