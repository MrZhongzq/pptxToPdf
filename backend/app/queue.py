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
