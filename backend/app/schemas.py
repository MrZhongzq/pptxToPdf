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
    engine: str | None = Field(default=None, min_length=1, max_length=32)
    """用户指定的引擎；None 表示交给 select_engine 自动判定。

    min_length=1：`engine=""` 之前会被 pipeline 的 `if requested:` 当成
    "没指定"而悄悄滑进 auto 分支——空字符串是客户端传参错误，不是"我没
    意见"，应该在请求校验这一层就挡掉，而不是被空字符串的假值悄悄吞掉。
    """
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
    shard_total: int | None = None
    """切片总数。None 表示未切片。"""
    shard_done: int = 0
    """已完成的分片数，由查询时现算——Task 上不存这个计数，见 models.Task
    上关于 shard_total 的注释：并发自增同一行在 SQLite 上要么加锁要么丢
    更新，TaskShard.status 才是唯一真相源。"""
    error_code: str | None
    error_message: str | None
    created_at: datetime


class ErrorResponse(BaseModel):
    """所有 API 错误的统一形状，码值取自 app/errors.py。"""

    code: str
    message: str


class CapacityConfig(BaseModel):
    """容量相关的只读配置，供前端在选 Graph 引擎时做上传前的启发式预判。

    只吐这几个纯数字，不吐任何凭证/配置状态（如 Graph 是否已配置）——那属于
    四期管理页范畴，也是信息泄露面，这轮不碰。四个字段直接来自
    app.config.settings 单例，端点里不另存一份，避免和后端实际配置漂移。
    """

    max_file_size: int
    graph_max_shards: int
    graph_max_shard_bytes: int
    graph_max_merge_bytes: int
