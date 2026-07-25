from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

MIB = 1024 * 1024


class Settings(BaseSettings):
    storage_root: Path = Path("storage")
    chunk_size: int = 5 * MIB
    max_file_size: int = 600 * MIB
    upload_ttl_hours: int = 24
    database_url: str = "sqlite:///./pptx2pdf.db"

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

    def ensure_dirs(self) -> None:
        for d in (self.uploads_dir, self.originals_dir, self.outputs_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
