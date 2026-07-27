import logging

from redis import Redis
from rq import Queue

from app.config import settings

logger = logging.getLogger(__name__)

QUEUE_NAME = "conversions"
# RQ 层的超时只是兜底，防 job 在 subprocess 之外的地方卡住。
# 引擎内部的 subprocess 超时（按页数算）才是主超时，会先触发，
# 那条路径我们能优雅处理：杀进程组、清 profile、落 CONVERSION_TIMEOUT。
JOB_TIMEOUT_MARGIN_S = 60


def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=Redis.from_url(settings.redis_url))


def enqueue_conversion(task_id: str) -> None:
    """把转换任务投进队列。

    job_timeout 用配置的最大值而非按页数算——enqueue 时还没 probe，
    不知道页数。这只是兜底上限，真正的超时在引擎内部。
    """
    from app.services.pipeline import run_task

    job_timeout = settings.convert_timeout_max_s + JOB_TIMEOUT_MARGIN_S
    job = get_queue().enqueue(run_task, task_id, job_timeout=job_timeout)
    logger.info("enqueued task=%s job=%s timeout=%ds", task_id, job.id, job_timeout)


def enqueue_shards(task_id: str, shard_ids: list[str]) -> None:
    """把每个分片作为独立 job 入队，再入队一个依赖它们全部的汇总 job。

    allow_failure=True 是必须的：默认的 False 会让任一分片失败时汇总 job
    永远停在 DeferredJobRegistry 里不执行，任务卡死在 converting，只能等
    孤儿回收器 45 分钟后收尸——而我们要的是立刻明确失败。汇总 job 自己检查
    每一行 TaskShard.status，有失败的就把 Task 标 failed。

    import 放在函数里，与 enqueue_conversion 同一个理由：app.services.
    shard_pipeline 顶层 import 了 app.services.pipeline，模块级引入会
    在 api 启动路径上多拖一层依赖。
    """
    from rq.job import Dependency

    from app.services.shard_pipeline import convert_shard, merge_shards

    q = get_queue()
    job_timeout = settings.convert_timeout_max_s + JOB_TIMEOUT_MARGIN_S
    shard_jobs = [
        q.enqueue(convert_shard, sid, job_timeout=job_timeout) for sid in shard_ids
    ]
    q.enqueue(
        merge_shards,
        task_id,
        depends_on=Dependency(jobs=shard_jobs, allow_failure=True),
        job_timeout=job_timeout,
    )
    logger.info("enqueued %d shards + merge for task=%s", len(shard_ids), task_id)
