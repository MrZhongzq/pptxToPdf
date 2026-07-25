from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import tasks, uploads
from app.config import settings
from app.db import init_db
from app.errors import AppError

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


@app.on_event("startup")
def startup() -> None:
    settings.ensure_dirs()
    init_db()


app.include_router(uploads.router)
app.include_router(tasks.router)
