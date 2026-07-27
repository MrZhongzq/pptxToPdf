from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PPTX2PDF_")

    # 一期既有
    storage_root: Path = Path("storage")
    chunk_size: int = 5 * MIB
    max_file_size: int = 600 * MIB
    upload_ttl_hours: int = 24
    database_url: str = "sqlite:///./pptx2pdf.db"

    # 二期新增：基础设施
    redis_url: str = "redis://redis:6379/0"
    output_ttl_hours: int = 24
    stale_task_minutes: int = 45

    # 二期新增：转换超时（秒）
    convert_timeout_base_s: int = 180
    convert_timeout_per_slide_s: int = 4
    convert_timeout_per_mb_s: int = 2
    convert_timeout_max_s: int = 1800
    soffice_bin: str = "soffice"

    # 三期：Graph 引擎
    secret_key: str | None = None
    """Fernet 主密钥（32 字节 urlsafe base64）。未配置则 Graph 引擎不可用——
    不设默认值兜底，那等于没加密。"""
    graph_max_pages_per_shard: int = 80
    graph_max_shard_bytes: int = 40 * MIB
    graph_request_timeout_s: int = 50
    graph_max_retries: int = 3
    graph_max_shards: int = 12
    """分片数上限。合并峰值内存正比于分片总量，13 片 × 40MB 外推约
    1.1-1.2GB，已经能在 2GB 内存的 worker 上触发 OOM。超过这个数宁可
    明确拒绝，也不能让 worker 被 OOM killer 静默干掉。"""
    graph_max_merge_bytes: int = 400 * MIB
    """合并输入（各分片 PDF）总字节上限。真正决定合并峰值内存的是 PDF 的
    总字节而不是分片数（一片也可能很大），所以分片数之外还要单独卡这一条。
    400MiB × 2.2 ≈ 880MB 峰值，给 2GB 的 worker 留足余量。"""

    # 二期新增：故障注入，默认全关
    debug_force_timeout: bool = False
    debug_force_engine_failure: bool = False
    debug_force_empty_output: bool = False
    debug_force_page_mismatch: bool = False

    @field_validator("storage_root")
    @classmethod
    def _resolve_storage_root(cls, v: Path) -> Path:
        """强制绝对化。task.output_path 落库时是 outputs_dir / f"{id}.pdf"——
        若 storage_root 保持相对路径，落库的就是相对 CWD 的字符串；uvicorn
        从不同工作目录重启后，tasks.py 里 Path(task.output_path).is_file()
        会按新 CWD 重新解析，历史任务的下载全部变成 409。这里在配置层一次性
        解决，pipeline.py 不需要再单独 resolve()。"""
        return v.resolve()

    @property
    def uploads_dir(self) -> Path:
        return self.storage_root / "uploads"

    @property
    def originals_dir(self) -> Path:
        return self.storage_root / "originals"

    @property
    def outputs_dir(self) -> Path:
        return self.storage_root / "outputs"

    @property
    def shards_dir(self) -> Path:
        return self.storage_root / "shards"

    def ensure_dirs(self) -> None:
        for d in (self.uploads_dir, self.originals_dir, self.outputs_dir, self.shards_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
