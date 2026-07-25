import shutil
import uuid
from pathlib import Path

from app.errors import StorageFull, UploadIncomplete

PART_SUFFIX = ".part"
ASSEMBLE_BUF = 1024 * 1024


class ChunkStore:
    """纯文件操作的块存储。不访问数据库，不认识 pptx。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self, upload_id: str) -> Path:
        return self.root / upload_id

    def _path(self, upload_id: str, index: int) -> Path:
        return self._dir(upload_id) / f"{index:06d}{PART_SUFFIX}"

    def save_chunk(self, upload_id: str, index: int, data: bytes) -> None:
        target = self._path(upload_id, index)
        # 唯一后缀，避免同一 index 的真并发重传在 tmp 层交叉写
        tmp = target.parent / f"{target.stem}.{uuid.uuid4().hex}.tmp"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            tmp.replace(target)  # 原子替换，重复投递天然幂等
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise StorageFull(f"写入块 {index} 失败: {exc}") from exc

    def _parts(self, upload_id: str) -> list[Path]:
        directory = self._dir(upload_id)
        if not directory.is_dir():
            return []
        return [
            p
            for p in directory.iterdir()
            if p.suffix == PART_SUFFIX and p.stem.isdigit()
        ]

    def received_indices(self, upload_id: str) -> set[int]:
        return {int(p.stem) for p in self._parts(upload_id)}

    def bytes_received(self, upload_id: str) -> int:
        return sum(p.stat().st_size for p in self._parts(upload_id))

    def assemble(self, upload_id: str, total_chunks: int, dest: Path) -> int:
        received = self.received_indices(upload_id)
        missing = sorted(set(range(total_chunks)) - received)
        if missing:
            preview = ", ".join(str(i) for i in missing[:10])
            raise UploadIncomplete(f"缺少 {len(missing)} 个块: {preview}")

        written = 0
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                for index in range(total_chunks):
                    with self._path(upload_id, index).open("rb") as part:
                        while chunk := part.read(ASSEMBLE_BUF):
                            out.write(chunk)
                            written += len(chunk)
        except OSError as exc:
            dest.unlink(missing_ok=True)
            raise StorageFull(f"拼装失败: {exc}") from exc
        return written

    def purge(self, upload_id: str) -> None:
        shutil.rmtree(self._dir(upload_id), ignore_errors=True)
