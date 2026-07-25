import hashlib
import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.errors import (
    StorageFull,
    UploadChecksumMismatch,
    UploadSessionExpired,
    UploadSessionNotFound,
    UploadSizeExceeded,
)
from app.models import Task, Upload
from app.schemas import (
    ChunkAck,
    CompleteResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    UploadStatus,
)
from app.services.chunk_store import ChunkStore
from app.services.pipeline import run_task

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


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

    一期不引入后台循环——新建上传是唯一会让磁盘增长的入口，
    在这里回收足以防止无限堆积。
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


@router.post("", response_model=CreateUploadResponse)
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


@router.put("/{upload_id}/chunks/{index}", response_model=ChunkAck)
async def put_chunk(
    upload_id: str,
    index: int,
    request: Request,
    session: Session = Depends(get_session),
) -> ChunkAck:
    upload = _load_active(session, upload_id)
    if not 0 <= index < upload.total_chunks:
        raise UploadSessionNotFound(f"块序号 {index} 越界")

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > upload.chunk_size:
        raise UploadSizeExceeded(
            f"块 {index} 声明 {declared} 字节，超过块大小 {upload.chunk_size}"
        )

    data = await request.body()
    if len(data) > upload.chunk_size:  # Content-Length 可能缺失或撒谎，读后复验兜底
        raise UploadSizeExceeded(f"块 {index} 为 {len(data)} 字节，超过块大小")

    chunks = store()
    chunks.save_chunk(upload_id, index, data)
    return ChunkAck(index=index, received_count=len(chunks.received_indices(upload_id)))


@router.get("/{upload_id}", response_model=UploadStatus)
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


@router.post("/{upload_id}/complete", response_model=CompleteResponse)
def complete_upload(
    upload_id: str,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> CompleteResponse:
    upload = _load_active(session, upload_id)
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
        status="pending",
        engine="placeholder",
    )
    session.add(task)
    session.commit()

    store().purge(upload_id)
    background.add_task(run_task, task_id)
    return CompleteResponse(task_id=task_id)
