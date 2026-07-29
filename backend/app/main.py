import logging
import sys

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin, admin_users, auth, config, tasks, uploads
from app.config import settings
from app.db import init_db
from app.errors import AppError, CrossOriginBlocked, ValidationError
from app.services import origin_guard
from app.services.retention import purge_expired_ready, purge_expired_shards, reap_stale_tasks
from app.services.users import bootstrap_admin

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


@app.middleware("http")
async def origin_guard_middleware(request: Request, call_next):
    """防跨站。默认关闭，见 services/origin_guard 的模块 docstring。

    两道保险都在这里体现：关闭时直接放行；开启但白名单为空时也放行——
    否则第一次打开开关就会把所有写请求（包括管理员自己添加白名单的那次
    请求）全部拒绝，变成一个无法自救的死锁。
    """
    if not settings.origin_guard_enabled or not origin_guard.should_check(request.method):
        return await call_next(request)

    from app.db import SessionLocal

    with SessionLocal() as session:
        allowed = origin_guard.load_allowed(session)
    if not allowed:
        return await call_next(request)

    host = origin_guard.extract_host(
        request.headers.get("origin") or request.headers.get("referer")
    )
    if not origin_guard.is_allowed(host, allowed):
        logger.warning("跨站请求被拒 host=%s path=%s", host, request.url.path)
        err = CrossOriginBlocked(f"来源 {host} 不在白名单中")
        return JSONResponse(
            status_code=err.http_status, content={"code": err.code, "message": err.message}
        )
    return await call_next(request)


@app.exception_handler(AppError)
def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    content = {"code": exc.code, "message": exc.message}
    steps = getattr(exc, "steps", None)
    if steps:
        content["steps"] = [
            step.model_dump() if hasattr(step, "model_dump") else dict(step)
            for step in steps
        ]
    return JSONResponse(
        status_code=exc.http_status,
        content=content,
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


def bootstrap_admin_account() -> None:
    """启动时确保 admin 账号存在。没配 PPTX2PDF_ADMIN_PASSWORD_HASH 就
    什么都不做——延续四期铁律，绝不生成默认密码兜底。"""
    from app.db import SessionLocal

    with SessionLocal() as session:
        bootstrap_admin(session)


@app.on_event("startup")
def startup() -> None:
    settings.ensure_dirs()
    init_db()
    bootstrap_admin_account()
    reap_stale_tasks()
    # 与上面 reap_stale_tasks 同一理由的双触发：OOM 之后最典型的运维动作
    # 就是重启，如果只挂 pipeline.run_task 那一半惰性清理，服务重启后长期
    # 没有新任务进来时，OOM killer 留下的分片目录残骸（数百 MB 量级）会
    # 永久占盘，没有任何路径会再碰它。
    purge_expired_shards()
    # 五期新增：传完但没点「开始转换」的 ready 任务同样只靠惰性清理——
    # 长期没有新任务时磁盘也没在涨，跟上面两条清理同一个「不需要额外
    # cron 容器」的理由。
    purge_expired_ready()


app.include_router(auth.router)
app.include_router(uploads.router)
app.include_router(tasks.router)
app.include_router(config.router)
app.include_router(admin.router)
app.include_router(admin_users.router)
