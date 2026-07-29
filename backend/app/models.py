from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    """登录标识，唯一。邮箱刻意不唯一——它只是联系方式，不参与鉴权，
    强制唯一会挡住「admin 给同一个人开测试号」这种正常用法。"""
    email: Mapped[str] = mapped_column(String(254))
    password_hash: Mapped[str] = mapped_column(String(256))
    """复用四期的 scrypt:<salt_hex>:<hash_hex>，见 services/auth.hash_password。"""
    role: Mapped[str] = mapped_column(String(16), default="user")
    """"admin" | "user"。只有两级，不做权限组。"""
    status: Mapped[str] = mapped_column(String(16), default="active")
    """"active" | "suspended"。删除是真删行，不做软删——否则「用户名唯一」
    会变得别扭：删掉的用户名到底还能不能再用。"""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AllowedOrigin(Base):
    """防跨站白名单。六期先建骨架，默认不启用（见 settings.origin_guard_enabled）。"""

    __tablename__ = "allowed_origins"

    origin_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    origin: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    """域名或 IP，可带端口。比对时只看 host:port，不看协议。"""
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Upload(Base):
    __tablename__ = "uploads"

    upload_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chunk_size: Mapped[int] = mapped_column(Integer)
    total_chunks: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")
    # 用户在上传时选的引擎与后处理选项，complete 时转写进 Task。
    # 放在 Upload 上是因为选项在选文件那一刻就定了，而 Task 要等
    # complete 才创建。
    requested_engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    options_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    upload_id: Mapped[str] = mapped_column(String(36))
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(Integer)
    slide_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slide_width_emu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slide_height_emu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fonts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    options_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 用户指定的引擎；None 表示交给 select_engine 自动判定。与 engine
    # 分开存，是为了让 run_task 保持只吃 task_id 就能自包含运行，
    # 不必回头去查 Upload 表。
    requested_engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 切片总数。None 表示不切片，走单次转换的原路径。
    # 不存已完成计数：多个分片并发完成时自增同一行在 SQLite 上要么加锁
    # 要么丢更新，而 TaskShard.status 本来就是这件事的唯一真相源。
    shard_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    engine: Mapped[str] = mapped_column(String(32), default="unassigned")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 「成功了，但有保留」——后处理只做到一半时的说明，JSON 数组。
    # 不能塞进 error_message：那个字段的存在即代表失败，前端据此渲染红色
    # 告警并停止轮询。动画展开跳过了几页仍然是一次成功的转换，用户拿得到
    # PDF，但必须明确知道哪几页没能展开，不能静默。
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 每页标题，JSON 数组，供 pdf_outline 后处理生成书签。
    # 必须在转换**前**提取并落库：分片路径下 run_task 的 finally 会
    # drop_original，等 merge_shards 跑到时原 pptx 已经不在了。
    outline_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TaskShard(Base):
    __tablename__ = "task_shards"

    shard_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    index: Mapped[int] = mapped_column(Integer)
    """0-based，决定合并顺序。"""
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    """原 deck 的页范围，1-based 闭区间。"""
    status: Mapped[str] = mapped_column(String(16), default="pending")
    output_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class GraphCredential(Base):
    """Azure AD 与 SharePoint 中转库的配置。单行表，id 恒为 1。

    三期只读（引擎用），四期的管理页面负责写入。client_secret 加密存储，
    主密钥在环境变量里——数据库文件在 volume 里，误备份或误提交都会泄露
    一个能操作 SharePoint 站点的凭证。
    """

    __tablename__ = "graph_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    tenant_id: Mapped[str] = mapped_column(String(64))
    client_id: Mapped[str] = mapped_column(String(64))
    client_secret_encrypted: Mapped[str] = mapped_column(Text)
    site_id: Mapped[str] = mapped_column(String(256))
    drive_path: Mapped[str] = mapped_column(String(256), default="pptx2pdf-staging")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
