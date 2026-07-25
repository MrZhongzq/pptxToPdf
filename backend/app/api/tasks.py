import json
from datetime import timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.errors import AppError
from app.models import Task
from app.schemas import TaskDto

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskNotFound(AppError):
    code = "TASK_NOT_FOUND"
    http_status = 404


class TaskNotReady(AppError):
    code = "TASK_NOT_READY"
    http_status = 409


def _load(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise TaskNotFound(f"任务 {task_id} 不存在")
    return task


def _fonts(task: Task) -> list[str]:
    """fonts_json 理论上只会是 probe() 写入的合法 JSON 数组或 None，
    但落库后的数据不受类型系统保护——防御性地把裸 JSONDecodeError
    （ValueError 的子类，不会被 AppError 处理器捕获）归一化成 AppError，
    避免返回不带错误码的裸 500。
    """
    if not task.fonts_json:
        return []
    try:
        return json.loads(task.fonts_json)
    except ValueError as exc:
        # 裸 json.JSONDecodeError 是 ValueError 的子类，不会被 AppError 处理器捕获，
        # 会变成不带错误码的 500——这里显式归一化为 AppError 契约。
        raise AppError(f"任务 {task.task_id} 的字体元数据已损坏") from exc


@router.get("/{task_id}", response_model=TaskDto)
def get_task(task_id: str, session: Session = Depends(get_session)) -> TaskDto:
    task = _load(session, task_id)
    return TaskDto(
        task_id=task.task_id,
        status=task.status,
        engine=task.engine,
        original_filename=task.original_filename,
        size_bytes=task.size_bytes,
        slide_count=task.slide_count,
        slide_width_emu=task.slide_width_emu,
        slide_height_emu=task.slide_height_emu,
        fonts=_fonts(task),
        error_code=task.error_code,
        error_message=task.error_message,
        # SQLite 不真的保留时区信息，读回来的是 naive datetime；直接序列化会
        # 输出不带 "Z" 后缀的 ISO 串，消费者一旦 new Date(...) 会按本地时区
        # 解析，在 UTC+8 偏 8 小时。与 uploads.py 的 _load_active 里对
        # expires_at 的同类补救保持一致处理。
        created_at=task.created_at.replace(tzinfo=timezone.utc),
    )


@router.get("/{task_id}/download")
def download(task_id: str, session: Session = Depends(get_session)) -> FileResponse:
    task = _load(session, task_id)
    if task.status != "done" or not task.output_path:
        raise TaskNotReady(f"任务状态为 {task.status}，尚无可下载结果")

    output_path = Path(task.output_path)
    if not output_path.is_file():
        raise TaskNotReady(f"任务 {task_id} 的结果文件已不存在")

    stem = Path(task.original_filename).stem
    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=f"{stem}.pdf",
    )
