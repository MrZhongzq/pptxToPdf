import json
import logging
from datetime import timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.errors import AppError, EngineUnavailable, ResultExpired
from app.models import Task, TaskShard
from app.queue import enqueue_conversion
from app.schemas import ConversionOptions, ErrorResponse, StartTaskRequest, TaskDto
from app.services.retention import drop_original

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_ERR = {"model": ErrorResponse}
TASK_ERRORS = {
    404: {**_ERR, "description": "TASK_NOT_FOUND"},
    409: {**_ERR, "description": "TASK_NOT_READY / TASK_ALREADY_STARTED"},
    410: {**_ERR, "description": "RESULT_EXPIRED"},
}


class TaskNotFound(AppError):
    code = "TASK_NOT_FOUND"
    http_status = 404


class TaskNotReady(AppError):
    code = "TASK_NOT_READY"
    http_status = 409


class TaskAlreadyStarted(AppError):
    """任务已经离开 ready 状态。

    不复用 TASK_NOT_READY——那个码已被 download 用于「还没转完、无可
    下载结果」，语义相反：一个是还没到终点，一个是已经离开起点。
    """

    code = "TASK_ALREADY_STARTED"
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


def _options(task: Task) -> ConversionOptions:
    """options_json 与 _fonts 同理：落库后的数据不受类型系统保护，
    损坏时归一化成默认选项而不是让裸异常穿透——后处理选项没有一项
    已实现，读不出来时按全关处理不会丢失任何用户可感知的行为。"""
    if not task.options_json:
        return ConversionOptions()
    try:
        return ConversionOptions.model_validate_json(task.options_json)
    except ValueError:
        logger.warning("任务 %s 的选项数据已损坏，按默认值处理", task.task_id)
        return ConversionOptions()


def _to_dto(session: Session, task: Task) -> TaskDto:
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
        options=_options(task),
        shard_total=task.shard_total,
        shard_done=(
            session.query(TaskShard)
            .filter(TaskShard.task_id == task.task_id, TaskShard.status == "done")
            .count()
            if task.shard_total
            else 0
        ),
        error_code=task.error_code,
        error_message=task.error_message,
        # SQLite 不真的保留时区信息，读回来的是 naive datetime；直接序列化会
        # 输出不带 "Z" 后缀的 ISO 串，消费者一旦 new Date(...) 会按本地时区
        # 解析，在 UTC+8 偏 8 小时。与 uploads.py 的 _load_active 里对
        # expires_at 的同类补救保持一致处理。
        created_at=task.created_at.replace(tzinfo=timezone.utc),
    )


@router.get("/{task_id}", response_model=TaskDto, responses=TASK_ERRORS)
def get_task(task_id: str, session: Session = Depends(get_session)) -> TaskDto:
    task = _load(session, task_id)
    return _to_dto(session, task)


@router.post("/{task_id}/start", response_model=TaskDto, responses=TASK_ERRORS)
def start_task(
    task_id: str,
    payload: StartTaskRequest,
    session: Session = Depends(get_session),
) -> TaskDto:
    task = _load(session, task_id)
    if task.status != "ready":
        raise TaskAlreadyStarted(f"任务状态为 {task.status}，无法重复启动")

    task.requested_engine = payload.engine
    if payload.options is not None:
        task.options_json = payload.options.model_dump_json()
    task.status = "pending"
    session.commit()

    try:
        enqueue_conversion(task_id)
    except Exception as exc:
        # 这段兜底从 complete_upload 挪过来。任务不会入队也就永远走不到
        # run_task 的 finally——那是原文件唯一的删除路径，不显式删就留下
        # 一份 80-500MB 的孤儿。
        task.status = "failed"
        task.error_code = EngineUnavailable.code
        task.error_message = f"任务排队失败，转换服务暂不可用: {exc}"
        session.commit()
        drop_original(task_id)
        raise EngineUnavailable(f"任务排队失败，转换服务暂不可用: {exc}") from exc

    session.refresh(task)
    return _to_dto(session, task)


@router.get("/{task_id}/download", responses=TASK_ERRORS)
def download(task_id: str, session: Session = Depends(get_session)) -> FileResponse:
    task = _load(session, task_id)
    if task.status != "done" or not task.output_path:
        raise TaskNotReady(f"任务状态为 {task.status}，尚无可下载结果")

    path = Path(task.output_path)
    if not path.is_file():
        # 任务确实成功过，但结果已被保留策略清理——这与「还没转完」
        # 是两回事，前端要据此提示用户重新上传而不是继续等。
        raise ResultExpired(
            f"结果文件已超过 {settings.output_ttl_hours} 小时保留期被清理，请重新上传"
        )

    stem = Path(task.original_filename).stem
    return FileResponse(str(path), media_type="application/pdf", filename=f"{stem}.pdf")


# HEAD 必须显式注册：Starlette 的 Route 会给 GET 自动补 HEAD，FastAPI 的
# APIRoute 不会——实测直接返回 405。前端下载前用 HEAD 做轻量预检，好在
# 真正传几十 MB 之前就拿到 410 RESULT_EXPIRED / 409 TASK_NOT_READY。
#
# include_in_schema=False：契约里 GET 已经描述了这个端点，HEAD 只是同一资源
# 的元数据查询；写进 schema 会和 GET 撞 operation id，也给读契约的人添噪音。
@router.head("/{task_id}/download", include_in_schema=False)
def download_head(
    task_id: str, session: Session = Depends(get_session)
) -> FileResponse:
    # 复用 GET 的全部校验：Starlette 的 FileResponse 认得 HEAD，只回头不回体。
    return download(task_id, session)
