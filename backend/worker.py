"""RQ worker 入口。容器 CMD 直接跑这个文件。

每个 job 由 RQ fork 出独立子进程执行，soffice 崩溃、内存泄漏、
段错误都被隔离在那个子进程里，不累积到常驻 worker 上。
"""

import logging
import sys

from redis import Redis
from rq import Worker

from app.config import settings
from app.db import init_db
from app.queue import QUEUE_NAME

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("worker")


def main() -> None:
    settings.ensure_dirs()
    init_db()

    for flag in (
        "debug_force_timeout",
        "debug_force_engine_failure",
        "debug_force_empty_output",
        "debug_force_page_mismatch",
    ):
        if getattr(settings, flag):
            logger.warning("故障注入开关已开启: PPTX2PDF_%s —— 生产环境请关闭", flag.upper())

    logger.info(
        "worker 启动 redis=%s storage=%s soffice=%s",
        settings.redis_url, settings.storage_root, settings.soffice_bin,
    )
    Worker([QUEUE_NAME], connection=Redis.from_url(settings.redis_url)).work()


if __name__ == "__main__":
    main()
