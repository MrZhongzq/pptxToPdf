import logging
import sys

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin, config, tasks, uploads
from app.config import settings
from app.db import init_db
from app.errors import AppError, ValidationError
from app.services.retention import purge_expired_shards, reap_stale_tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

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


@app.on_event("startup")
def startup() -> None:
    settings.ensure_dirs()
    init_db()
    reap_stale_tasks()
    # 与上面 reap_stale_tasks 同一理由的双触发：OOM 之后最典型的运维动作
    # 就是重启，如果只挂 pipeline.run_task 那一半惰性清理，服务重启后长期
    # 没有新任务进来时，OOM killer 留下的分片目录残骸（数百 MB 量级）会
    # 永久占盘，没有任何路径会再碰它。
    purge_expired_shards()


app.include_router(uploads.router)
app.include_router(tasks.router)
app.include_router(config.router)
app.include_router(admin.router)
