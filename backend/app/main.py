import logging
import sys
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import tasks, uploads
from app.config import settings
from app.db import SessionLocal, init_db
from app.errors import AppError, TaskAbandoned, ValidationError
from app.models import Task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

NON_TERMINAL = ("pending", "parsing", "queued", "converting")

app = FastAPI(title="pptx2pdf", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"code": exc.code, "message": exc.message},
    )


def _summarize_validation_errors(errors: list[dict]) -> str:
    """把 pydantic 的 exc.errors() 摘要成一行人类可读的文字。

    原始结构是一组 {loc, msg, type} 字典，直接透传给前端只会让 api.ts 的
    parse() 找不到 body.code，退化成显示英文 "Unprocessable Entity"。
    """
    parts = []
    for err in errors:
        loc = ".".join(str(p) for p in err.get("loc", []) if p != "body")
        msg = err.get("msg", "请求参数不合法")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "；".join(parts) if parts else "请求参数不合法"


@app.exception_handler(RequestValidationError)
def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    err = ValidationError(_summarize_validation_errors(exc.errors()))
    return JSONResponse(
        status_code=err.http_status,
        content={"code": err.code, "message": err.message},
    )


def reap_stale_tasks() -> int:
    """把卡在非终态太久的任务标为失败，返回回收数量。

    RQ 的 job 只活在 worker 进程里。进程被 kill（部署、OOM、崩溃）
    时任务会永远停在中间态，而前端按异步轮询设计，会一直轮询一个
    永不改变的状态。上游设计 §10.3 定的方向是显性要求用户重传。

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


@app.on_event("startup")
def startup() -> None:
    settings.ensure_dirs()
    init_db()
    reap_stale_tasks()


app.include_router(uploads.router)
app.include_router(tasks.router)
