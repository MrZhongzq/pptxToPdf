from pathlib import Path

from pydantic_settings import BaseSettings

MIB = 1024 * 1024


class Settings(BaseSettings):
    storage_root: Path = Path("storage")
    chunk_size: int = 5 * MIB
    max_file_size: int = 600 * MIB
    upload_ttl_hours: int = 24
    database_url: str = "sqlite:///./pptx2pdf.db"

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
