import json
import logging
from datetime import datetime, timezone

from app.config import settings
from app.db import SessionLocal
from app.errors import AppError, GraphNotConfigured
from app.models import Task
from app.queue import enqueue_shards
from app.services.animation_expand import expand_animations
from app.services.engine_router import select_engine
from app.services.engines import get_engine
from app.services.graph_credentials import is_graph_configured
from app.services.media_strip import strip_media
from app.services.postprocess import apply as apply_postprocess
from app.services.pptx_probe import extract_titles, probe
from app.services.task_options import options_of, titles_of
from app.services.retention import (
    drop_original,
    purge_expired_outputs,
    purge_expired_ready,
    purge_expired_shards,
    reap_stale_tasks,
)
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
            # 剥离内嵌媒体。放在 probe 之后：probe 提取的四个字段
            # （slide_count / slide_width_emu / slide_height_emu / fonts）
            # 全部来自 slides / slideMasters / slideLayouts / theme /
            # presentation.xml 这些 part，媒体剥离只删 video/audio/media
            # part 并逐字节流式复制其余内容，对这四个字段免疫——剥离放
            # probe 前后不影响 probe 的结果，但 probe 自己的校验（加密 /
            # 非法 zip / 不是演示文稿）必须先跑完，用户才能拿到准确的
            # 错误码；strip_media 没有这层校验，先跑会把 PptxEncrypted /
            # PptxInvalidZip 这类可诊断的错误钝化成一个笼统的 BadZipFile。
            # size_bytes 本就在 probe 之后，剥离插在这里，它自然读到剥离
            # 后的体积。PDF 放不了视频——这些字节留着只会让 size_bytes
            # 虚高，把本来单次能转的 deck 推进切片路径甚至撞上分片上限。
            # 剥离后的文件覆盖原件，此后所有判断
            # （size_bytes / needs_sharding / 切片 / 转换）都基于它。
            strip = strip_media(src)
            if strip.stripped:
                logger.info(
                    "media stripped id=%s parts=%d %d -> %d bytes",
                    task_id, strip.removed_parts, strip.bytes_before, strip.bytes_after,
                )
            # 动画展开同样必须在 size_bytes 之前：它会改变页数，而页数与
            # 体积正是切片规划的两个输入。展开放在剥离之后——剥离让包变小，
            # 展开会按步数复制 slide 正文，先剥离再展开复制的是更小的东西。
            options = options_of(task)
            if options.expand_animations:
                expanded = expand_animations(src)
                if expanded.expanded:
                    logger.info(
                        "animations expanded id=%s %d -> %d pages",
                        task_id, expanded.pages_before, expanded.pages_after,
                    )
                    meta = probe(src)  # 页数变了，重新探测
                if expanded.warnings:
                    task.warnings_json = json.dumps(expanded.warnings, ensure_ascii=False)

            # 书签标题必须在这里提取并落库：分片路径下本函数的 finally 会
            # drop_original，等 merge_shards 跑到时原 pptx 已经不在了。
            if options.pdf_outline:
                task.outline_json = json.dumps(extract_titles(src), ensure_ascii=False)

            size_bytes = src.stat().st_size
            # 剥离后的体积取代上传时记的原始体积——task.size_bytes 从这里
            # 起是"参与转换判断的那个值"，不再是用户上传时的原始大小。
            task.size_bytes = size_bytes
            task.slide_count = meta.slide_count
            task.slide_width_emu = meta.slide_width_emu
            task.slide_height_emu = meta.slide_height_emu
            task.fonts_json = json.dumps(list(meta.fonts), ensure_ascii=False)
            # 是否已配置 Graph 只影响 auto 分支（select_engine 内部保证
            # requested 分支绝不受这个信号影响）——三期没有凭证写入路径，
            # 这里在三期部署里恒为 False，auto 因此恒选 libreoffice；
            # 下面的分片分支复用同一个值做早退检查（I2），避免整轮切片
            # 之后才在每个子 job 里第一次发现凭证没配置。
            graph_configured = is_graph_configured(session)
            task.engine = select_engine(
                meta,
                size_bytes,
                requested=task.requested_engine,
                graph_configured=graph_configured,
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
                from app.services.shard_pipeline import discard_shards, prepare_shards

                if not graph_configured:
                    # I2：凭证检查必须在切片之前做。不然一个 500 页的 deck
                    # 会先完整切一轮（最多 480MB 落盘、commit 几十行
                    # TaskShard、入队几十个 RQ job），才在每个子 job 里
                    # 第一次读凭证时发现根本没配置——用户等几分钟磁盘写
                    # 近 1GB，换来一句本可以在毫秒内给出的错误。
                    # graph_configured 在上面已经算过一次，这里复用同一个
                    # 值，不再多打一次 DB。
                    raise GraphNotConfigured(
                        "尚未配置 Azure 凭证，无法执行需要切片的 Graph 转换。"
                        "请在管理页面配置凭证，或改用 LibreOffice 引擎重新上传"
                    )

                shard_ids = prepare_shards(session, task, src, size_bytes)
                try:
                    enqueue_shards(task_id, shard_ids)
                except Exception:
                    # 入队失败（Redis 不可用）时，行已 commit、最多 480MB 的
                    # 分片 pptx 已落盘，而 merge_shards 永远不会被触发来清它们。
                    # 必须在这里把 prepare_shards 的副作用整个撤掉，再让异常
                    # 照常传出去落 failed。
                    discard_shards(session, task_id)
                    raise
                logger.info(
                    "task sharded id=%s shards=%d，转换与合并交给子 job",
                    task_id, len(shard_ids),
                )
                return

            timeout_s = compute_timeout_s(meta.slide_count, size_bytes)
            # session= 让 get_engine 在需要时（graph 引擎）就地读凭证并注入
            # 构造函数——引擎本身不允许碰数据库，见 engines/base.py。
            get_engine(task.engine, session=session).convert(
                src, meta, dest, timeout_s=timeout_s
            )

            apply_postprocess(dest, options, titles_of(task))

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
        # purge_expired_outputs 只扫 outputs_dir；分片路径的中间产物落在
        # shards_dir，正常路径靠 merge_shards / discard_shards 自己清，这里
        # 补的是两者都够不到的残骸（worker 在分片子 job 中途被 OOM killer
        # 干掉，没有任何 finally 会跑）。与上面同一惰性模式，顺带触发一次。
        removed_shards = purge_expired_shards()
        if removed_shards:
            logger.info("retention 清理了 %d 个过期分片目录", removed_shards)
        # 只在 api 启动时回收孤儿任务不够：worker 容器有内存上限，OOM 是
        # 预期事件，work-horse 被杀后 api 未必会重启，回收器就可能永远不跑。
        # 这里顺带触发一次，与上面 purge_expired_outputs() 同一个惰性模式。
        reap_stale_tasks()
        # 五期新增：与上面三条清理同一惰性模式——ready 任务超时未启动的
        # 回收不需要单独的 cron 容器，搭一次转换任务收尾的顺风车即可。
        purge_expired_ready()
        session.close()
