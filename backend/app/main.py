from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import tasks, uploads
from app.config import settings
from app.db import init_db
from app.errors import AppError, ValidationError

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


app.include_router(uploads.router)
app.include_router(tasks.router)
