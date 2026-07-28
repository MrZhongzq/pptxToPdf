import hashlib
import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.errors import (
    EngineUnavailable,
    StorageFull,
    UploadChecksumMismatch,
    UploadSessionExpired,
    UploadSessionNotActive,
    UploadSessionNotFound,
    UploadSizeExceeded,
)
from app.models import Task, Upload
from app.schemas import (
    ChunkAck,
    CompleteResponse,
    ConversionOptions,
    CreateUploadRequest,
    CreateUploadResponse,
    ErrorResponse,
    UploadStatus,
)
from app.queue import enqueue_conversion
from app.services.chunk_store import ChunkStore
from app.services.retention import drop_original

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

_ERR = {"model": ErrorResponse}
UPLOAD_ERRORS = {
    404: {**_ERR, "description": "UPLOAD_SESSION_NOT_FOUND"},
    409: {**_ERR, "description": "UPLOAD_INCOMPLETE / UPLOAD_SESSION_NOT_ACTIVE"},
    410: {**_ERR, "description": "UPLOAD_SESSION_EXPIRED"},
    413: {**_ERR, "description": "UPLOAD_SIZE_EXCEEDED"},
    422: {**_ERR, "description": "VALIDATION_ERROR / UPLOAD_CHECKSUM_MISMATCH"},
    507: {**_ERR, "description": "STORAGE_FULL"},
}


def store() -> ChunkStore:
    return ChunkStore(settings.uploads_dir)


HASH_BLOCK = 1024 * 1024


def _load_active(session: Session, upload_id: str) -> Upload:
    upload = session.get(Upload, upload_id)
    if upload is None:
        raise UploadSessionNotFound(f"上传会话 {upload_id} 不存在")
    if upload.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise UploadSessionExpired(f"上传会话 {upload_id} 已过期")
    return upload


def _sha256_of(path: Path) -> str:
    """流式摘要，500MB 文件也不会把内容读进内存。"""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while block := fh.read(HASH_BLOCK):
                digest.update(block)
    except OSError as exc:
        raise StorageFull(f"读取 {path} 计算校验和失败: {exc}") from exc
    return digest.hexdigest()


def _purge_expired(session: Session) -> None:
    """惰性清理：每次新建会话时顺带回收过期会话的块目录。

    注意范围：本函数只回收 uploads/ 下过期会话的块目录。originals/
    （拼装后的原始 pptx）与 outputs/（转换产物 PDF）的保留策略由
    services/retention.py 负责——run_task 结束时调 drop_original() 删原文件，
    每次任务结束顺带调 purge_expired_outputs() 清过期 PDF。
    """
    now = datetime.now(timezone.utc)
    stale = (
        session.query(Upload)
        .filter(Upload.status == "active", Upload.expires_at < now)
        .all()
    )
    if not stale:
        return
    chunks = store()
    for upload in stale:
        chunks.purge(upload.upload_id)
        upload.status = "expired"
    session.commit()


@router.post("", response_model=CreateUploadResponse, responses=UPLOAD_ERRORS)
def create_upload(
    body: CreateUploadRequest, session: Session = Depends(get_session)
) -> CreateUploadResponse:
    if body.size > settings.max_file_size:
        raise UploadSizeExceeded(
            f"文件 {body.size} 字节，超过上限 {settings.max_file_size} 字节"
        )

    _purge_expired(session)

    upload = Upload(
        upload_id=str(uuid.uuid4()),
        filename=body.filename,
        size_bytes=body.size,
        sha256=body.sha256,
        chunk_size=settings.chunk_size,
        total_chunks=math.ceil(body.size / settings.chunk_size),
        requested_engine=body.engine,
        options_json=(body.options or ConversionOptions()).model_dump_json(),
        expires_at=datetime.now(timezone.utc)
        + timedelta(hours=settings.upload_ttl_hours),
    )
    session.add(upload)
    session.commit()

    return CreateUploadResponse(
        upload_id=upload.upload_id,
        chunk_size=upload.chunk_size,
        total_chunks=upload.total_chunks,
        expires_at=upload.expires_at,
    )


@router.put("/{upload_id}/chunks/{index}", response_model=ChunkAck, responses=UPLOAD_ERRORS)
async def put_chunk(
    upload_id: str,
    index: int,
    request: Request,
    session: Session = Depends(get_session),
) -> ChunkAck:
    upload = _load_active(session, upload_id)
    if upload.status != "active":
        # complete 之后迟到的 PUT（网络层重传、abort 后仍在飞的请求）不得
        # 复活块目录——那个目录一旦复活，_purge_expired 只清 active 会话，
        # 永远不会被任何路径回收。
        raise UploadSessionNotActive(
            f"上传会话 {upload_id} 状态为 {upload.status}，不再接受分片"
        )
    if not 0 <= index < upload.total_chunks:
        raise UploadSessionNotFound(f"块序号 {index} 越界")

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > upload.chunk_size:
        raise UploadSizeExceeded(
            f"块 {index} 声明 {declared} 字节，超过块大小 {upload.chunk_size}"
        )

    # 流式读取并在超限时立即中断。Content-Length 缺失时
    # （Transfer-Encoding: chunked）await request.body() 是无上限的，
    # 校验发生在整个 body already 进内存之后，起不到防护作用。
    buffer = bytearray()
    async for part in request.stream():
        buffer.extend(part)
        if len(buffer) > upload.chunk_size:
            raise UploadSizeExceeded(
                f"块 {index} 实际超过块大小 {upload.chunk_size} 字节"
            )
    data = bytes(buffer)

    chunks = store()
    chunks.save_chunk(upload_id, index, data)
    return ChunkAck(index=index, received_count=len(chunks.received_indices(upload_id)))


@router.get("/{upload_id}", response_model=UploadStatus, responses=UPLOAD_ERRORS)
def get_status(
    upload_id: str, session: Session = Depends(get_session)
) -> UploadStatus:
    upload = _load_active(session, upload_id)
    chunks = store()
    return UploadStatus(
        received_indices=sorted(chunks.received_indices(upload_id)),
        bytes_received=chunks.bytes_received(upload_id),
        total_chunks=upload.total_chunks,
        chunk_size=upload.chunk_size,
        status=upload.status,
    )


# 只有本端点会在 Redis 不可达时抛 EngineUnavailable(503)，所以 503 就地合并进
# responses，而不是加到共享的 UPLOAD_ERRORS 里给另外三个端点误声明。
@router.post(
    "/{upload_id}/complete",
    response_model=CompleteResponse,
    responses={**UPLOAD_ERRORS, 503: {**_ERR, "description": "ENGINE_UNAVAILABLE"}},
)
def complete_upload(
    upload_id: str,
    session: Session = Depends(get_session),
) -> CompleteResponse:
    upload = _load_active(session, upload_id)
    if upload.status != "active":
        raise UploadSessionNotActive(
            f"上传会话 {upload_id} 状态为 {upload.status}，无法重复完成"
        )
    task_id = str(uuid.uuid4())
    dest = settings.originals_dir / f"{task_id}.pptx"

    written = store().assemble(upload_id, upload.total_chunks, dest)
    if written != upload.size_bytes:
        dest.unlink(missing_ok=True)
        raise UploadChecksumMismatch(
            f"拼装得到 {written} 字节，声明为 {upload.size_bytes} 字节"
        )
    if upload.sha256 and _sha256_of(dest).lower() != upload.sha256.lower():
        dest.unlink(missing_ok=True)
        raise UploadChecksumMismatch("SHA-256 与声明不符，文件可能在传输中损坏")

    upload.status = "completed"
    task = Task(
        task_id=task_id,
        upload_id=upload_id,
        original_filename=upload.filename,
        size_bytes=upload.size_bytes,
        options_json=upload.options_json,
        requested_engine=upload.requested_engine,
        status="pending",
    )
    # engine 仍留 "unassigned"：用户指定的引擎名带在 upload 上，
    # 由 run_task 在 probe 之后交给 select_engine 定夺后再写进 task。
    session.add(task)
    session.commit()

    store().purge(upload_id)
    try:
        enqueue_conversion(task_id)
    except Exception as exc:
        # Redis 不可达时 Queue.enqueue 抛的是 redis.exceptions.ConnectionError，
        # 不是 AppError——main.py 的处理器接不住，会退化成裸文本 500，违反
        # 错误契约。这里兜底捕获任意异常（队列实现将来可能换，不锁定具体
        # 异常类型）。此时 upload 已标 completed、块目录已 purge、原文件
        # 已落盘、task 行是 pending：任务永远不会入队，也就永远不会走到
        # run_task 的 finally 里的 drop_original——这里是原文件唯一的删除
        # 路径，必须显式调用，否则留下一份 80–500MB 的孤儿文件。
        task.status = "failed"
        task.error_code = EngineUnavailable.code
        task.error_message = f"任务排队失败，转换服务暂不可用: {exc}"
        session.commit()
        drop_original(task_id)
        raise EngineUnavailable(f"任务排队失败，转换服务暂不可用: {exc}") from exc

    return CompleteResponse(task_id=task_id)
