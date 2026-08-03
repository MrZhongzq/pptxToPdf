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


class StartTaskRequest(BaseModel):
    """引擎与选项在 start 时才提交——上传时不必先想好。"""

    engine: str | None = Field(default=None, min_length=1, max_length=32)
    options: ConversionOptions | None = None


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


class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class GraphCredentialsDto(BaseModel):
    """读配置的响应。client_secret 绝不出现在这里——解密回显等于把凭证
    明文发到浏览器，那么加密存库本身就失去意义。"""

    tenant_id: str
    client_id: str
    site_id: str
    drive_path: str
    secret_configured: bool


class GraphCredentialsUpdate(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    client_id: str = Field(min_length=1, max_length=64)
    client_secret: str = Field(default="", max_length=512)
    """留空表示沿用库中已存的值。首次配置时留空会被拒绝。"""
    site_id: str = Field(min_length=1, max_length=256)
    drive_path: str = Field(default="pptx2pdf-staging", min_length=1, max_length=256)


class SelftestStepDto(BaseModel):
    step: str
    ok: bool | None
    detail: str | None


class SelftestResultDto(BaseModel):
    ok: bool
    steps: list[SelftestStepDto]


# ---- 六期：账号 ----


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserDto(BaseModel):
    """对外的用户视图。**永远不含 password_hash**——哈希不是秘密，但把它
    发到浏览器等于把离线爆破的入场券送出去，而它没有任何前端用途。"""

    user_id: str
    username: str
    email: str
    role: str
    status: str
    created_at: datetime


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(default="user", pattern="^(admin|user)$")


class SetUserStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|suspended)$")


class SetUserPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)


# ---- 六期：防跨站白名单 ----


class AllowedOriginDto(BaseModel):
    origin_id: str
    origin: str
    note: str | None
    created_at: datetime


class CreateAllowedOriginRequest(BaseModel):
    origin: str = Field(min_length=1, max_length=256)
    note: str | None = Field(default=None, max_length=256)


# ---- 六期：系统状态 ----


class SystemStatsDto(BaseModel):
    tasks_total: int
    tasks_by_status: dict[str, int]
    users_total: int
    storage_originals_bytes: int
    storage_outputs_bytes: int
    storage_shards_bytes: int


# ---- 字体管理 ----


class FontFaceDto(BaseModel):
    family: str
    style: str


class FontFileDto(BaseModel):
    file_id: str
    filename: str
    source: str
    families: list[str]
    faces: list[FontFaceDto]
    version: str
    charset_count: int
    size_bytes: int
    modified_at: datetime
    deletable: bool


class FontListDto(BaseModel):
    managed: list[FontFileDto]
    mounted: list[FontFileDto]
    builtin: list[FontFileDto]


class FontPreflightDto(BaseModel):
    token: str
    incoming: FontFileDto
    #: sha256 完全相同的已有文件。非空时前端不该再弹冲突框，直接提示已存在。
    duplicate_of: FontFileDto | None
    #: family 有重叠的已有文件，由管理员逐个决定是否替换。
    candidates: list[FontFileDto]


class FontCommitRequest(BaseModel):
    #: preflight 返回的暂存 token。必须锁死格式——它会被拿去拼路径，
    #: 而 pathlib 的 `base / "/etc"` 会整体替换掉 base（见 font_store.py
    #: 里 decode_file_id 的注释，同一个陷阱我们踩过一次了）。token 恒为
    #: uuid.uuid4().hex，即 32 位小写十六进制。
    token: str = Field(pattern=r"^[0-9a-f]{32}$")
    #: 要被替换掉的已有字体的 file_id。空列表表示「这是一个新字体」。
    replace: list[str] = []
