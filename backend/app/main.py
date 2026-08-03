import logging
import sys

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin, admin_fonts, admin_users, auth, config, tasks, uploads, v1
from app.config import settings
from app.db import init_db
from app.errors import AppError, CrossOriginBlocked, OriginBlocked, ValidationError
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
async def access_control_middleware(request: Request, call_next):
    """黑名单（全站）与来源白名单（只管 v1）。

    顺序是承重的：**黑名单在最前**，命中即 403 返回，不再进入任何后续
    判断——这正是需求要的「优先级高于防跨站」。

    白名单只作用于 /v1/*。网页永远不受它影响，所以即使白名单拒绝一切，
    webui 照常工作；也正因如此，六期那道「白名单为空时放行」的保险在
    七期被去掉了——空白名单现在的语义是「v1 谁也不许用」。
    """
    from app.db import SessionLocal

    path = request.url.path
    client_ip = origin_guard.client_ip(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
    )
    hosts = origin_guard.candidate_hosts(
        client_ip, request.headers.get("origin"), request.headers.get("referer")
    )

    with SessionLocal() as session:
        blocked = origin_guard.load_blocked(session)
        hit = origin_guard.match_any(blocked, hosts) if blocked else None
        if hit is not None:
            logger.warning("黑名单拦截 hosts=%s rule=%s path=%s", hosts, hit.raw, path)
            err = OriginBlocked("访问被拒绝")
            return JSONResponse(
                status_code=err.http_status, content={"code": err.code, "message": err.message}
            )

        if origin_guard.is_v1(path) and settings.origin_guard_enabled:
            allowed = origin_guard.load_allowed(session)
            rule = origin_guard.match_any(allowed, hosts)
            if rule is None:
                logger.warning("v1 来源不在白名单 hosts=%s path=%s", hosts, path)
                err = CrossOriginBlocked("来源不在 v1 白名单中")
                return JSONResponse(
                    status_code=err.http_status,
                    content={"code": err.code, "message": err.message},
                )
            # @no_graph 是修饰符，不影响放行，只影响放行之后能不能选 graph。
            # 挂在 request.state 上交给 v1 端点判断——中间件不该知道引擎参数
            # 叫什么名字。
            request.state.origin_rule = rule

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
app.include_router(admin_fonts.router)
app.include_router(v1.router)
