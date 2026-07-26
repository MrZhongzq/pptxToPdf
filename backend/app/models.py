from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    status: Mapped[str] = mapped_column(String(16), default="pending")
    engine: Mapped[str] = mapped_column(String(32), default="unassigned")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
