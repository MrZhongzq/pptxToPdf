from datetime import datetime

from pydantic import BaseModel, Field


class CreateUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=1)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class CreateUploadResponse(BaseModel):
    upload_id: str
    chunk_size: int
    total_chunks: int
    expires_at: datetime


class ChunkAck(BaseModel):
    index: int
    received_count: int


class UploadStatus(BaseModel):
    received_indices: list[int]
    bytes_received: int
    total_chunks: int
    chunk_size: int
    status: str


class CompleteResponse(BaseModel):
    task_id: str


class TaskDto(BaseModel):
    task_id: str
    status: str
    engine: str
    original_filename: str
    size_bytes: int
    slide_count: int | None
    slide_width_emu: int | None
    slide_height_emu: int | None
    fonts: list[str]
    error_code: str | None
    error_message: str | None
    created_at: datetime


class ErrorResponse(BaseModel):
    """所有 API 错误的统一形状，码值取自 app/errors.py。"""

    code: str
    message: str
