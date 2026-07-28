from datetime import datetime

from pydantic import BaseModel, Field


class ConversionOptions(BaseModel):
    """转换后处理选项。

    后端目前只接收并落库，不实现任何一项——字段先定下来，
    将来实现时不用再改前后端契约。前端会在每项旁标注实现状态。
    """

    expand_animations: bool = False
    """按 p:timing 把动画 build step 展开成多页，避免所有元素叠在一页上。"""

    pdf_outline: bool = False
    """用每页标题生成 PDF 书签大纲，便于在 GoodNotes 里跳转。"""

    remap_margins: bool = False
    """页边距重映射，给 iPad 竖屏批注留出侧边空白。"""


class CreateUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=1)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    engine: str | None = Field(default=None, max_length=32)
    """用户指定的引擎；None 表示交给 select_engine 自动判定。"""
    options: ConversionOptions | None = None


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
    options: ConversionOptions
    error_code: str | None
    error_message: str | None
    created_at: datetime


class ErrorResponse(BaseModel):
    """所有 API 错误的统一形状，码值取自 app/errors.py。"""

    code: str
    message: str
