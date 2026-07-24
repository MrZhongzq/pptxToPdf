# 一期：前端 + 上传骨架 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成一个能真实上传 500MB pptx（分片、断点续传）、解析出页数与尺寸、并返回页数尺寸都正确的占位 PDF 的三端可用站点。

**Architecture:** React SPA 通过自实现的四端点分片协议把文件切成 5 MiB 块并发上传到 FastAPI；后端把块落盘、拼装、用 zipfile 轻量解析元信息、交给可插拔的 `ConversionEngine`（一期是 `PlaceholderEngine`）产出 PDF。前端全程按异步轮询模型工作，二期换真引擎时前端零改动。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / SQLite / reportlab / pytest；React 19 / Vite / TypeScript / vitest

## Global Constraints

- 单文件硬上限 **600 MB**（`600 * 1024 * 1024`）。前端拦截仅为体验，后端必须复校验。
- 块大小 **5 MiB**（`5 * 1024 * 1024`），客户端并发 **3** 块。
- 数据库是元数据的**唯一真相源**。文件系统只存字节，不写 `meta.json` 之类旁路元数据。
- `ChunkStore` **不得访问数据库**；`pptx_probe` **不得访问 HTTP**；`ConversionEngine` **不得访问数据库**。
- **禁止用 `python-pptx.Presentation()` 解析元信息**——它会把整包读入内存，500MB 文件会吃掉数 GB。生产代码只用 `zipfile` 读需要的条目。`python-pptx` 仅可作为 **dev 依赖**用于生成测试样本。
- 任务状态机固定为 `pending → parsing → queued → converting → done`，任一环节可转 `failed`。占位引擎虽瞬时完成也必须走完全部状态。
- 所有 API 错误返回 `{"code": "...", "message": "..."}`，码值取自 `app/errors.py`。
- 后端所有路径操作用 `pathlib.Path`，不用字符串拼接。

---

### Task 1: 后端脚手架、配置、错误码与数据模型

**Files:**
- Create: `backend/requirements.txt`, `backend/requirements-dev.txt`
- Create: `backend/app/__init__.py`, `backend/app/config.py`, `backend/app/errors.py`, `backend/app/db.py`, `backend/app/models.py`, `backend/app/main.py`
- Create: `backend/tests/__init__.py`, `backend/tests/conftest.py`, `backend/tests/test_models.py`
- Create: `backend/pytest.ini`, `.gitignore`

**Interfaces:**
- Consumes: 无
- Produces:
  - `app.config.Settings` 带字段 `storage_root: Path`, `chunk_size: int`, `max_file_size: int`, `upload_ttl_hours: int`, `database_url: str`；模块级单例 `settings`
  - `app.errors.AppError(code: str, http_status: int, message: str)` 及其子类 `UploadSessionNotFound`, `UploadSessionExpired`, `UploadSizeExceeded`, `UploadIncomplete`, `UploadChecksumMismatch`, `PptxInvalidZip`, `PptxNotPresentation`, `PptxEncrypted`, `StorageFull`
  - `app.db.Base`, `app.db.get_session()`, `app.db.init_db()`
  - `app.models.Upload`, `app.models.Task`

- [ ] **Step 1: 创建依赖文件与 gitignore**

`backend/requirements.txt`：
```
fastapi==0.115.6
uvicorn[standard]==0.34.0
sqlalchemy==2.0.36
pydantic==2.10.4
pydantic-settings==2.7.0
reportlab==4.2.5
python-multipart==0.0.20
```

`backend/requirements-dev.txt`：
```
-r requirements.txt
pytest==8.3.4
pytest-asyncio==0.25.0
httpx==0.28.1
python-pptx==1.0.2
```

`.gitignore`（仓库根）：
```
__pycache__/
*.pyc
.venv/
venv/
node_modules/
dist/
storage/
*.db
.pytest_cache/
.vite/
```

`backend/pytest.ini`：
```ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

- [ ] **Step 2: 写配置模块**

`backend/app/config.py`：
```python
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
```

- [ ] **Step 3: 写错误码模块**

`backend/app/errors.py`：
```python
class AppError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message: str = "") -> None:
        self.message = message or self.code
        super().__init__(self.message)


class UploadSessionNotFound(AppError):
    code = "UPLOAD_SESSION_NOT_FOUND"
    http_status = 404


class UploadSessionExpired(AppError):
    code = "UPLOAD_SESSION_EXPIRED"
    http_status = 410


class UploadSizeExceeded(AppError):
    code = "UPLOAD_SIZE_EXCEEDED"
    http_status = 413


class UploadIncomplete(AppError):
    code = "UPLOAD_INCOMPLETE"
    http_status = 409


class UploadChecksumMismatch(AppError):
    code = "UPLOAD_CHECKSUM_MISMATCH"
    http_status = 422


class PptxInvalidZip(AppError):
    code = "PPTX_INVALID_ZIP"
    http_status = 422


class PptxNotPresentation(AppError):
    code = "PPTX_NOT_PRESENTATION"
    http_status = 422


class PptxEncrypted(AppError):
    code = "PPTX_ENCRYPTED"
    http_status = 422


class StorageFull(AppError):
    code = "STORAGE_FULL"
    http_status = 507
```

- [ ] **Step 4: 写数据库与模型**

`backend/app/db.py`：
```python
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from app import models  # noqa: F401  确保模型已注册到 Base.metadata

    Base.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
```

`backend/app/models.py`：
```python
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
    status: Mapped[str] = mapped_column(String(16), default="pending")
    engine: Mapped[str] = mapped_column(String(32), default="placeholder")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
```

- [ ] **Step 5: 写测试夹具与首个测试**

`backend/tests/conftest.py`：
```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    s = maker()
    yield s
    s.close()


@pytest.fixture
def storage_root(tmp_path):
    root = tmp_path / "storage"
    for sub in ("uploads", "originals", "outputs"):
        (root / sub).mkdir(parents=True)
    return root
```

`backend/tests/test_models.py`：
```python
from datetime import datetime, timedelta, timezone

from app.models import Task, Upload


def test_upload_roundtrip(session):
    up = Upload(
        upload_id="u1",
        filename="deck.pptx",
        size_bytes=1024,
        sha256=None,
        chunk_size=5 * 1024 * 1024,
        total_chunks=1,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(up)
    session.commit()

    loaded = session.get(Upload, "u1")
    assert loaded.filename == "deck.pptx"
    assert loaded.status == "active"


def test_task_defaults(session):
    task = Task(
        task_id="t1",
        upload_id="u1",
        original_filename="deck.pptx",
        size_bytes=1024,
    )
    session.add(task)
    session.commit()

    loaded = session.get(Task, "t1")
    assert loaded.status == "pending"
    assert loaded.engine == "placeholder"
    assert loaded.user_id is None
```

- [ ] **Step 6: 运行测试确认通过**

Run:
```bash
cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -v
```
Expected: 2 passed

- [ ] **Step 7: 提交**

```bash
git add .gitignore backend/
git commit -m "feat(backend): 脚手架、配置、错误码与数据模型"
```

---

### Task 2: ChunkStore 块存储层

**Files:**
- Create: `backend/app/services/__init__.py`, `backend/app/services/chunk_store.py`
- Test: `backend/tests/test_chunk_store.py`

**Interfaces:**
- Consumes: `app.errors.UploadIncomplete`, `app.errors.StorageFull`
- Produces:
  - `ChunkStore(root: Path)`
  - `.save_chunk(upload_id: str, index: int, data: bytes) -> None`
  - `.received_indices(upload_id: str) -> set[int]`
  - `.bytes_received(upload_id: str) -> int`
  - `.assemble(upload_id: str, total_chunks: int, dest: Path) -> int`（返回写入字节数，缺块时抛 `UploadIncomplete`）
  - `.purge(upload_id: str) -> None`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_chunk_store.py`：
```python
import pytest

from app.errors import UploadIncomplete
from app.services.chunk_store import ChunkStore


def test_assemble_out_of_order(storage_root, tmp_path):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 2, b"ccc")
    store.save_chunk("u1", 0, b"aaa")
    store.save_chunk("u1", 1, b"bbb")

    dest = tmp_path / "out.bin"
    written = store.assemble("u1", 3, dest)

    assert written == 9
    assert dest.read_bytes() == b"aaabbbccc"


def test_received_indices(storage_root):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 5, b"x")
    store.save_chunk("u1", 0, b"y")

    assert store.received_indices("u1") == {0, 5}


def test_received_indices_unknown_upload(storage_root):
    store = ChunkStore(storage_root / "uploads")
    assert store.received_indices("nope") == set()


def test_bytes_received(storage_root):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 0, b"abcd")
    store.save_chunk("u1", 3, b"xy")

    assert store.bytes_received("u1") == 6
    assert store.bytes_received("nope") == 0


def test_duplicate_chunk_is_idempotent(storage_root, tmp_path):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 0, b"first")
    store.save_chunk("u1", 0, b"SECOND")

    dest = tmp_path / "out.bin"
    store.assemble("u1", 1, dest)

    assert dest.read_bytes() == b"SECOND"


def test_assemble_missing_chunk_raises(storage_root, tmp_path):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 0, b"aaa")

    with pytest.raises(UploadIncomplete) as exc:
        store.assemble("u1", 3, tmp_path / "out.bin")

    assert exc.value.code == "UPLOAD_INCOMPLETE"
    assert "1" in exc.value.message and "2" in exc.value.message


def test_purge_removes_all_chunks(storage_root):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 0, b"aaa")
    store.purge("u1")

    assert store.received_indices("u1") == set()
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_chunk_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.chunk_store'`

- [ ] **Step 3: 实现 ChunkStore**

`backend/app/services/chunk_store.py`：
```python
import shutil
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
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        try:
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

        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
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
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_chunk_store.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/ backend/tests/test_chunk_store.py
git commit -m "feat(backend): ChunkStore 支持乱序、幂等与缺块检测"
```

---

### Task 3: pptx 元信息解析

**Files:**
- Create: `backend/app/services/pptx_probe.py`
- Test: `backend/tests/test_pptx_probe.py`

**Interfaces:**
- Consumes: `app.errors.PptxInvalidZip`, `PptxNotPresentation`, `PptxEncrypted`
- Produces:
  - `PptxMeta`（frozen dataclass）字段 `slide_count: int`, `slide_width_emu: int`, `slide_height_emu: int`, `fonts: tuple[str, ...]`
  - `probe(path: Path) -> PptxMeta`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_pptx_probe.py`：
```python
import zipfile

import pytest
from pptx import Presentation
from pptx.util import Emu

from app.errors import PptxEncrypted, PptxInvalidZip, PptxNotPresentation
from app.services.pptx_probe import probe

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@pytest.fixture
def sample_pptx(tmp_path):
    """用 python-pptx 造样本——仅测试用，生产代码不得依赖它。"""
    prs = Presentation()
    prs.slide_width = Emu(12192000)   # 16:9
    prs.slide_height = Emu(6858000)
    for i in range(3):
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = f"第 {i + 1} 页"
    path = tmp_path / "sample.pptx"
    prs.save(path)
    return path


def test_probe_slide_count_and_size(sample_pptx):
    meta = probe(sample_pptx)

    assert meta.slide_count == 3
    assert meta.slide_width_emu == 12192000
    assert meta.slide_height_emu == 6858000


def test_probe_collects_fonts(sample_pptx):
    meta = probe(sample_pptx)

    assert isinstance(meta.fonts, tuple)
    assert all(isinstance(f, str) for f in meta.fonts)
    assert meta.fonts == tuple(sorted(meta.fonts))


def test_probe_rejects_non_zip(tmp_path):
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"this is not a zip file at all")

    with pytest.raises(PptxInvalidZip):
        probe(bad)


def test_probe_rejects_encrypted(tmp_path):
    enc = tmp_path / "enc.pptx"
    enc.write_bytes(CFB_MAGIC + b"\x00" * 128)

    with pytest.raises(PptxEncrypted):
        probe(enc)


def test_probe_rejects_zip_without_presentation(tmp_path):
    zpath = tmp_path / "notppt.pptx"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("hello.txt", "world")

    with pytest.raises(PptxNotPresentation):
        probe(zpath)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_pptx_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.pptx_probe'`

- [ ] **Step 3: 实现 probe**

`backend/app/services/pptx_probe.py`：
```python
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from app.errors import PptxEncrypted, PptxInvalidZip, PptxNotPresentation

# OOXML 加密文件是 OLE 复合文档，不是 zip
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

PRESENTATION_PART = "ppt/presentation.xml"
SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")
FONT_SCAN_RE = re.compile(r"^ppt/(slides|slideMasters|slideLayouts|theme)/.+\.xml$")

P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
FONT_TAGS = (f"{A_NS}latin", f"{A_NS}ea", f"{A_NS}cs")

MAX_FONT_PARTS = 200  # 超长 deck 只扫前若干个 part，字体清单不需要穷举


@dataclass(frozen=True)
class PptxMeta:
    slide_count: int
    slide_width_emu: int
    slide_height_emu: int
    fonts: tuple[str, ...]


def _is_encrypted(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(8) == CFB_MAGIC


def _read_slide_size(zf: zipfile.ZipFile) -> tuple[int, int]:
    try:
        raw = zf.read(PRESENTATION_PART)
    except KeyError as exc:
        raise PptxNotPresentation("缺少 ppt/presentation.xml") from exc

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise PptxNotPresentation(f"presentation.xml 解析失败: {exc}") from exc

    sld_sz = root.find(f"{P_NS}sldSz")
    if sld_sz is None:
        raise PptxNotPresentation("presentation.xml 缺少 sldSz")
    return int(sld_sz.attrib["cx"]), int(sld_sz.attrib["cy"])


def _collect_fonts(zf: zipfile.ZipFile) -> tuple[str, ...]:
    fonts: set[str] = set()
    parts = [n for n in zf.namelist() if FONT_SCAN_RE.match(n)][:MAX_FONT_PARTS]
    for name in parts:
        try:
            root = ET.fromstring(zf.read(name))
        except (ET.ParseError, KeyError):
            continue  # 单个 part 坏掉不应让整次解析失败
        for tag in FONT_TAGS:
            for el in root.iter(tag):
                typeface = el.attrib.get("typeface", "").strip()
                # 跳过 +mj-lt / +mn-ea 这类主题占位引用
                if typeface and not typeface.startswith("+"):
                    fonts.add(typeface)
    return tuple(sorted(fonts))


def probe(path: Path) -> PptxMeta:
    """只读所需 zip 条目解析元信息，内存开销与文件大小无关。"""
    path = Path(path)
    if _is_encrypted(path):
        raise PptxEncrypted("文件已加密，无法解析")

    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            slide_count = sum(1 for n in names if SLIDE_RE.match(n))
            if PRESENTATION_PART not in names:
                raise PptxNotPresentation("不是 PowerPoint 演示文稿")
            width, height = _read_slide_size(zf)
            fonts = _collect_fonts(zf)
    except zipfile.BadZipFile as exc:
        raise PptxInvalidZip(f"不是合法的 zip 容器: {exc}") from exc

    return PptxMeta(
        slide_count=slide_count,
        slide_width_emu=width,
        slide_height_emu=height,
        fonts=fonts,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_pptx_probe.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/pptx_probe.py backend/tests/test_pptx_probe.py
git commit -m "feat(backend): zipfile 轻量解析 pptx 元信息"
```

---

### Task 4: 引擎抽象与 PlaceholderEngine

**Files:**
- Create: `backend/app/services/engines/__init__.py`, `backend/app/services/engines/base.py`, `backend/app/services/engines/placeholder.py`
- Test: `backend/tests/test_placeholder_engine.py`

**Interfaces:**
- Consumes: `app.services.pptx_probe.PptxMeta`
- Produces:
  - `ConversionEngine`（ABC）类属性 `name: str`，抽象方法 `convert(src: Path, meta: PptxMeta, dest: Path) -> None`
  - `PlaceholderEngine`（`name = "placeholder"`）
  - `EMU_PER_POINT = 12700`
  - `get_engine(name: str) -> ConversionEngine`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_placeholder_engine.py`：
```python
import pytest
from pypdf import PdfReader

from app.services.engines import get_engine
from app.services.engines.base import EMU_PER_POINT
from app.services.engines.placeholder import PlaceholderEngine
from app.services.pptx_probe import PptxMeta


@pytest.fixture
def meta():
    return PptxMeta(
        slide_count=3,
        slide_width_emu=12192000,
        slide_height_emu=6858000,
        fonts=("Calibri",),
    )


def test_placeholder_page_count(tmp_path, meta):
    dest = tmp_path / "out.pdf"
    PlaceholderEngine().convert(tmp_path / "unused.pptx", meta, dest)

    assert len(PdfReader(dest).pages) == 3


def test_placeholder_page_size_matches_slide(tmp_path, meta):
    dest = tmp_path / "out.pdf"
    PlaceholderEngine().convert(tmp_path / "unused.pptx", meta, dest)

    page = PdfReader(dest).pages[0]
    expected_w = meta.slide_width_emu / EMU_PER_POINT
    expected_h = meta.slide_height_emu / EMU_PER_POINT
    assert float(page.mediabox.width) == pytest.approx(expected_w, abs=1.0)
    assert float(page.mediabox.height) == pytest.approx(expected_h, abs=1.0)


def test_placeholder_has_selectable_text(tmp_path, meta):
    dest = tmp_path / "out.pdf"
    PlaceholderEngine().convert(tmp_path / "unused.pptx", meta, dest)

    text = PdfReader(dest).pages[0].extract_text()
    assert "1 / 3" in text


def test_get_engine_returns_placeholder():
    assert get_engine("placeholder").name == "placeholder"


def test_get_engine_unknown_raises():
    with pytest.raises(KeyError):
        get_engine("libreoffice")
```

- [ ] **Step 2: 把 pypdf 加入 dev 依赖并运行确认失败**

在 `backend/requirements-dev.txt` 末尾追加：
```
pypdf==5.1.0
```

Run:
```bash
.venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest tests/test_placeholder_engine.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.engines'`

- [ ] **Step 3: 实现引擎抽象与占位实现**

`backend/app/services/engines/base.py`：
```python
from abc import ABC, abstractmethod
from pathlib import Path

from app.services.pptx_probe import PptxMeta

EMU_PER_POINT = 12700


class ConversionEngine(ABC):
    """转换引擎抽象。不得访问数据库，不得认识 HTTP。"""

    name: str

    @abstractmethod
    def convert(self, src: Path, meta: PptxMeta, dest: Path) -> None:
        """把 src 转成 PDF 写到 dest。失败时抛异常。"""
```

`backend/app/services/engines/placeholder.py`：
```python
from pathlib import Path

from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from app.services.engines.base import EMU_PER_POINT, ConversionEngine
from app.services.pptx_probe import PptxMeta


class PlaceholderEngine(ConversionEngine):
    """一期占位引擎：页数与页面尺寸真实，内容为占位文字。"""

    name = "placeholder"

    def convert(self, src: Path, meta: PptxMeta, dest: Path) -> None:
        width = meta.slide_width_emu / EMU_PER_POINT
        height = meta.slide_height_emu / EMU_PER_POINT

        dest.parent.mkdir(parents=True, exist_ok=True)
        pdf = canvas.Canvas(str(dest), pagesize=(width, height))
        for page in range(1, meta.slide_count + 1):
            pdf.setFont("Helvetica", 28)
            pdf.drawCentredString(
                width / 2, height / 2, f"{page} / {meta.slide_count}"
            )
            pdf.setFont("Helvetica", 12)
            pdf.drawCentredString(
                width / 2, height / 2 - 0.5 * inch, "engine not wired yet (phase 1)"
            )
            pdf.showPage()
        pdf.save()
```

`backend/app/services/engines/__init__.py`：
```python
from app.services.engines.base import ConversionEngine
from app.services.engines.placeholder import PlaceholderEngine

_ENGINES: dict[str, ConversionEngine] = {
    PlaceholderEngine.name: PlaceholderEngine(),
}


def get_engine(name: str) -> ConversionEngine:
    """按名取引擎。二期在 _ENGINES 里加一行即可接入 LibreOffice。"""
    return _ENGINES[name]


__all__ = ["ConversionEngine", "PlaceholderEngine", "get_engine"]
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_placeholder_engine.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/engines/ backend/tests/test_placeholder_engine.py backend/requirements-dev.txt
git commit -m "feat(backend): 可插拔引擎抽象与占位 PDF 引擎"
```

---

### Task 5: 上传 API 四端点

**Files:**
- Create: `backend/app/schemas.py`, `backend/app/api/__init__.py`, `backend/app/api/uploads.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_uploads_api.py`

**Interfaces:**
- Consumes: `ChunkStore`, `Upload`, `Task`, 全部 `AppError` 子类, `settings`
- Produces:
  - `POST /api/uploads` → `CreateUploadResponse{upload_id, chunk_size, total_chunks, expires_at}`
  - `PUT /api/uploads/{upload_id}/chunks/{index}` → `ChunkAck{index, received_count}`
  - `GET /api/uploads/{upload_id}` → `UploadStatus{received_indices, bytes_received, total_chunks, chunk_size, status}`
  - `POST /api/uploads/{upload_id}/complete` → `CompleteResponse{task_id}`
  - `app.services.pipeline.run_task(task_id: str) -> None`
  - `app.main.app`（FastAPI 实例，已注册 `AppError` 异常处理器）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_uploads_api.py`：
```python
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import Base, SessionLocal, engine


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "chunk_size", 4)  # 小块便于测试
    settings.ensure_dirs()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create(client, size: int, filename="deck.pptx"):
    resp = await client.post(
        "/api/uploads", json={"filename": filename, "size": size}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_create_computes_total_chunks(client):
    body = await _create(client, size=10)  # 10 字节 / 4 字节块 = 3 块
    assert body["total_chunks"] == 3
    assert body["chunk_size"] == 4


async def test_create_rejects_oversize(client):
    resp = await client.post(
        "/api/uploads",
        json={"filename": "huge.pptx", "size": settings.max_file_size + 1},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "UPLOAD_SIZE_EXCEEDED"


async def test_status_reports_received_indices(client):
    body = await _create(client, size=10)
    uid = body["upload_id"]

    await client.put(f"/api/uploads/{uid}/chunks/2", content=b"gh")
    await client.put(f"/api/uploads/{uid}/chunks/0", content=b"abcd")

    resp = await client.get(f"/api/uploads/{uid}")
    assert resp.json()["received_indices"] == [0, 2]
    assert resp.json()["bytes_received"] == 6
    assert resp.json()["chunk_size"] == 4  # 客户端续传时靠它切分，不得自行反推


async def test_status_unknown_upload(client):
    resp = await client.get("/api/uploads/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["code"] == "UPLOAD_SESSION_NOT_FOUND"


async def test_complete_with_missing_chunk(client):
    body = await _create(client, size=10)
    uid = body["upload_id"]
    await client.put(f"/api/uploads/{uid}/chunks/0", content=b"abcd")

    resp = await client.post(f"/api/uploads/{uid}/complete")
    assert resp.status_code == 409
    assert resp.json()["code"] == "UPLOAD_INCOMPLETE"


async def test_complete_size_mismatch(client):
    body = await _create(client, size=10)
    uid = body["upload_id"]
    for idx, payload in enumerate([b"abcd", b"efgh", b"XXXX"]):  # 实际 12 != 声明 10
        await client.put(f"/api/uploads/{uid}/chunks/{idx}", content=payload)

    resp = await client.post(f"/api/uploads/{uid}/complete")
    assert resp.status_code == 422
    assert resp.json()["code"] == "UPLOAD_CHECKSUM_MISMATCH"


async def test_complete_sha256_mismatch(client):
    resp = await client.post(
        "/api/uploads",
        json={"filename": "deck.pptx", "size": 4, "sha256": "0" * 64},
    )
    uid = resp.json()["upload_id"]
    await client.put(f"/api/uploads/{uid}/chunks/0", content=b"abcd")

    done = await client.post(f"/api/uploads/{uid}/complete")
    assert done.status_code == 422
    assert done.json()["code"] == "UPLOAD_CHECKSUM_MISMATCH"


async def test_complete_sha256_match(client):
    import hashlib

    payload = b"abcd"
    resp = await client.post(
        "/api/uploads",
        json={
            "filename": "deck.pptx",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    uid = resp.json()["upload_id"]
    await client.put(f"/api/uploads/{uid}/chunks/0", content=payload)

    done = await client.post(f"/api/uploads/{uid}/complete")
    assert done.status_code == 200


async def test_expired_sessions_are_purged_on_create(client):
    from datetime import datetime, timedelta, timezone

    from app.db import SessionLocal
    from app.models import Upload

    first = (
        await client.post("/api/uploads", json={"filename": "old.pptx", "size": 4})
    ).json()
    await client.put(f"/api/uploads/{first['upload_id']}/chunks/0", content=b"abcd")

    # 把会话强制置为已过期
    session = SessionLocal()
    stale = session.get(Upload, first["upload_id"])
    stale.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.commit()
    session.close()

    await client.post("/api/uploads", json={"filename": "new.pptx", "size": 4})

    session = SessionLocal()
    assert session.get(Upload, first["upload_id"]).status == "expired"
    session.close()
    assert not (settings.uploads_dir / first["upload_id"]).exists()


async def test_complete_returns_task_id(client):
    body = await _create(client, size=10)
    uid = body["upload_id"]
    for idx, payload in enumerate([b"abcd", b"efgh", b"ij"]):
        await client.put(f"/api/uploads/{uid}/chunks/{idx}", content=payload)

    resp = await client.post(f"/api/uploads/{uid}/complete")
    assert resp.status_code == 200
    assert resp.json()["task_id"]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_uploads_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api'`

- [ ] **Step 3: 写 Pydantic 模型**

`backend/app/schemas.py`：
```python
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
```

- [ ] **Step 4: 实现上传路由**

`backend/app/api/uploads.py`：
```python
import hashlib
import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.errors import (
    UploadChecksumMismatch,
    UploadSessionExpired,
    UploadSessionNotFound,
    UploadSizeExceeded,
)
from app.models import Task, Upload
from app.schemas import (
    ChunkAck,
    CompleteResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    UploadStatus,
)
from app.services.chunk_store import ChunkStore
from app.services.pipeline import run_task

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def store() -> ChunkStore:
    return ChunkStore(settings.uploads_dir)


HASH_BLOCK = 1024 * 1024


def _load_active(session: Session, upload_id: str) -> Upload:
    upload = session.get(Upload, upload_id)
    if upload is None:
        raise UploadSessionNotFound(f"上传会话 {upload_id} 不存在")
    if upload.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise UploadSessionExpired(f"上传会话 {upload_id} 已过期")
    return upload


def _sha256_of(path: Path) -> str:
    """流式摘要，500MB 文件也不会把内容读进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def _purge_expired(session: Session) -> None:
    """惰性清理：每次新建会话时顺带回收过期会话的块目录。

    一期不引入后台循环——新建上传是唯一会让磁盘增长的入口，
    在这里回收足以防止无限堆积。
    """
    now = datetime.now(timezone.utc)
    stale = (
        session.query(Upload)
        .filter(Upload.status == "active", Upload.expires_at < now)
        .all()
    )
    if not stale:
        return
    chunks = store()
    for upload in stale:
        chunks.purge(upload.upload_id)
        upload.status = "expired"
    session.commit()


@router.post("", response_model=CreateUploadResponse)
def create_upload(
    body: CreateUploadRequest, session: Session = Depends(get_session)
) -> CreateUploadResponse:
    if body.size > settings.max_file_size:
        raise UploadSizeExceeded(
            f"文件 {body.size} 字节，超过上限 {settings.max_file_size} 字节"
        )

    _purge_expired(session)

    upload = Upload(
        upload_id=str(uuid.uuid4()),
        filename=body.filename,
        size_bytes=body.size,
        sha256=body.sha256,
        chunk_size=settings.chunk_size,
        total_chunks=math.ceil(body.size / settings.chunk_size),
        expires_at=datetime.now(timezone.utc)
        + timedelta(hours=settings.upload_ttl_hours),
    )
    session.add(upload)
    session.commit()

    return CreateUploadResponse(
        upload_id=upload.upload_id,
        chunk_size=upload.chunk_size,
        total_chunks=upload.total_chunks,
        expires_at=upload.expires_at,
    )


@router.put("/{upload_id}/chunks/{index}", response_model=ChunkAck)
async def put_chunk(
    upload_id: str,
    index: int,
    request: Request,
    session: Session = Depends(get_session),
) -> ChunkAck:
    upload = _load_active(session, upload_id)
    if not 0 <= index < upload.total_chunks:
        raise UploadSessionNotFound(f"块序号 {index} 越界")

    data = await request.body()
    if len(data) > upload.chunk_size:
        raise UploadSizeExceeded(f"块 {index} 为 {len(data)} 字节，超过块大小")

    chunks = store()
    chunks.save_chunk(upload_id, index, data)
    return ChunkAck(index=index, received_count=len(chunks.received_indices(upload_id)))


@router.get("/{upload_id}", response_model=UploadStatus)
def get_status(
    upload_id: str, session: Session = Depends(get_session)
) -> UploadStatus:
    upload = _load_active(session, upload_id)
    chunks = store()
    return UploadStatus(
        received_indices=sorted(chunks.received_indices(upload_id)),
        bytes_received=chunks.bytes_received(upload_id),
        total_chunks=upload.total_chunks,
        chunk_size=upload.chunk_size,
        status=upload.status,
    )


@router.post("/{upload_id}/complete", response_model=CompleteResponse)
def complete_upload(
    upload_id: str,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> CompleteResponse:
    upload = _load_active(session, upload_id)
    task_id = str(uuid.uuid4())
    dest = settings.originals_dir / f"{task_id}.pptx"

    written = store().assemble(upload_id, upload.total_chunks, dest)
    if written != upload.size_bytes:
        dest.unlink(missing_ok=True)
        raise UploadChecksumMismatch(
            f"拼装得到 {written} 字节，声明为 {upload.size_bytes} 字节"
        )
    if upload.sha256 and _sha256_of(dest).lower() != upload.sha256.lower():
        dest.unlink(missing_ok=True)
        raise UploadChecksumMismatch("SHA-256 与声明不符，文件可能在传输中损坏")

    upload.status = "completed"
    task = Task(
        task_id=task_id,
        upload_id=upload_id,
        original_filename=upload.filename,
        size_bytes=upload.size_bytes,
        status="pending",
        engine="placeholder",
    )
    session.add(task)
    session.commit()

    store().purge(upload_id)
    background.add_task(run_task, task_id)
    return CompleteResponse(task_id=task_id)
```

- [ ] **Step 5: 实现任务流水线（供 Task 6 复用）**

`backend/app/services/pipeline.py`：
```python
import json

from app.config import settings
from app.db import SessionLocal
from app.errors import AppError
from app.models import Task
from app.services.engines import get_engine
from app.services.pptx_probe import probe


def _set_status(session, task: Task, status: str) -> None:
    task.status = status
    session.commit()


def run_task(task_id: str) -> None:
    """走完整状态机：parsing → queued → converting → done / failed。"""
    session = SessionLocal()
    try:
        task = session.get(Task, task_id)
        if task is None:
            return

        src = settings.originals_dir / f"{task_id}.pptx"
        try:
            _set_status(session, task, "parsing")
            meta = probe(src)
            task.slide_count = meta.slide_count
            task.slide_width_emu = meta.slide_width_emu
            task.slide_height_emu = meta.slide_height_emu
            task.fonts_json = json.dumps(list(meta.fonts), ensure_ascii=False)

            _set_status(session, task, "queued")
            _set_status(session, task, "converting")

            dest = settings.outputs_dir / f"{task_id}.pdf"
            get_engine(task.engine).convert(src, meta, dest)

            task.output_path = str(dest)
            _set_status(session, task, "done")
        except AppError as exc:
            task.error_code = exc.code
            task.error_message = exc.message
            _set_status(session, task, "failed")
        except Exception as exc:  # noqa: BLE001  兜底，避免后台任务静默吞掉
            task.error_code = "INTERNAL_ERROR"
            task.error_message = str(exc)
            _set_status(session, task, "failed")
    finally:
        session.close()
```

- [ ] **Step 6: 装配 main.py**

`backend/app/main.py`：
```python
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import tasks, uploads
from app.config import settings
from app.db import init_db
from app.errors import AppError

app = FastAPI(title="pptx2pdf", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"code": exc.code, "message": exc.message},
    )


@app.on_event("startup")
def startup() -> None:
    settings.ensure_dirs()
    init_db()


app.include_router(uploads.router)
app.include_router(tasks.router)
```

创建 `backend/app/api/__init__.py`（空文件）。

**注意：** 本步引用了 Task 6 才创建的 `app.api.tasks`。先建一个最小占位以便本任务测试可跑：

`backend/app/api/tasks.py`（Task 6 会替换全部内容）：
```python
from fastapi import APIRouter

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
```

- [ ] **Step 7: 运行确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_uploads_api.py -v`
Expected: 10 passed

- [ ] **Step 8: 提交**

```bash
git add backend/app/api/ backend/app/schemas.py backend/app/main.py backend/app/services/pipeline.py backend/tests/test_uploads_api.py
git commit -m "feat(backend): 分片上传四端点与任务流水线"
```

---

### Task 6: 任务查询与结果下载

**Files:**
- Modify: `backend/app/api/tasks.py`（替换 Task 5 的占位内容）
- Test: `backend/tests/test_tasks_api.py`

**Interfaces:**
- Consumes: `Task`, `TaskDto`, `settings`
- Produces:
  - `GET /api/tasks/{task_id}` → `TaskDto`
  - `GET /api/tasks/{task_id}/download` → `application/pdf` 文件响应

- [ ] **Step 1: 写失败测试**

`backend/tests/test_tasks_api.py`：
```python
import pytest
from httpx import ASGITransport, AsyncClient
from pptx import Presentation
from pptx.util import Emu
from pypdf import PdfReader

from app.config import settings
from app.db import Base, engine


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "chunk_size", 64 * 1024)
    settings.ensure_dirs()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def sample_bytes(tmp_path):
    prs = Presentation()
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(6858000)
    for _ in range(4):
        prs.slides.add_slide(prs.slide_layouts[6])
    path = tmp_path / "s.pptx"
    prs.save(path)
    return path.read_bytes()


async def _upload(client, payload: bytes) -> str:
    created = (
        await client.post(
            "/api/uploads", json={"filename": "deck.pptx", "size": len(payload)}
        )
    ).json()
    uid, size = created["upload_id"], created["chunk_size"]
    for idx in range(created["total_chunks"]):
        await client.put(
            f"/api/uploads/{uid}/chunks/{idx}",
            content=payload[idx * size : (idx + 1) * size],
        )
    return (await client.post(f"/api/uploads/{uid}/complete")).json()["task_id"]


async def test_task_reaches_done_with_parsed_meta(client, sample_bytes):
    task_id = await _upload(client, sample_bytes)

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "done"
    assert body["slide_count"] == 4
    assert body["slide_width_emu"] == 12192000
    assert body["engine"] == "placeholder"
    assert isinstance(body["fonts"], list)


async def test_download_returns_pdf_with_matching_pages(client, sample_bytes, tmp_path):
    task_id = await _upload(client, sample_bytes)

    resp = await client.get(f"/api/tasks/{task_id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"

    out = tmp_path / "got.pdf"
    out.write_bytes(resp.content)
    assert len(PdfReader(out).pages) == 4


async def test_task_not_found(client):
    resp = await client.get("/api/tasks/nope")
    assert resp.status_code == 404


async def test_invalid_pptx_marks_failed(client):
    task_id = await _upload(client, b"definitely not a zip file")

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "PPTX_INVALID_ZIP"


async def test_download_before_done_returns_409(client):
    task_id = await _upload(client, b"definitely not a zip file")

    resp = await client.get(f"/api/tasks/{task_id}/download")
    assert resp.status_code == 409
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_tasks_api.py -v`
Expected: FAIL — 404，路由尚未实现

- [ ] **Step 3: 实现任务路由**

`backend/app/api/tasks.py`（整文件替换）：
```python
import json
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.errors import AppError
from app.models import Task
from app.schemas import TaskDto

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskNotFound(AppError):
    code = "TASK_NOT_FOUND"
    http_status = 404


class TaskNotReady(AppError):
    code = "TASK_NOT_READY"
    http_status = 409


def _load(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise TaskNotFound(f"任务 {task_id} 不存在")
    return task


@router.get("/{task_id}", response_model=TaskDto)
def get_task(task_id: str, session: Session = Depends(get_session)) -> TaskDto:
    task = _load(session, task_id)
    return TaskDto(
        task_id=task.task_id,
        status=task.status,
        engine=task.engine,
        original_filename=task.original_filename,
        size_bytes=task.size_bytes,
        slide_count=task.slide_count,
        slide_width_emu=task.slide_width_emu,
        slide_height_emu=task.slide_height_emu,
        fonts=json.loads(task.fonts_json) if task.fonts_json else [],
        error_code=task.error_code,
        error_message=task.error_message,
        created_at=task.created_at,
    )


@router.get("/{task_id}/download")
def download(task_id: str, session: Session = Depends(get_session)) -> FileResponse:
    task = _load(session, task_id)
    if task.status != "done" or not task.output_path:
        raise TaskNotReady(f"任务状态为 {task.status}，尚无可下载结果")

    stem = Path(task.original_filename).stem
    return FileResponse(
        task.output_path,
        media_type="application/pdf",
        filename=f"{stem}.pdf",
    )
```

- [ ] **Step 4: 运行全部后端测试确认通过**

Run: `.venv/Scripts/python -m pytest -v`
Expected: 全部 passed（约 30 项）

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/tasks.py backend/tests/test_tasks_api.py
git commit -m "feat(backend): 任务查询与 PDF 下载端点"
```

---

### Task 7: 前端脚手架与液态玻璃设计系统

**Files:**
- Create: `frontend/package.json`, `frontend/vite.config.ts`, `frontend/tsconfig.json`, `frontend/index.html`
- Create: `frontend/src/main.tsx`, `frontend/src/App.tsx`
- Create: `frontend/src/styles/tokens.css`, `frontend/src/styles/glass.css`, `frontend/src/styles/global.css`

**Interfaces:**
- Consumes: 无
- Produces: CSS 类 `.glass`, `.glass-strong`, `.glass-inset`；CSS 变量前缀 `--g-*`；Vite dev server 代理 `/api` → `http://127.0.0.1:8000`

- [ ] **Step 1: 用 Vite 生成脚手架并装依赖**

Run:
```bash
npm create vite@latest frontend -- --template react-ts
cd frontend && npm install && npm install -D vitest jsdom @testing-library/react @testing-library/jest-dom
```

- [ ] **Step 2: 配置 Vite 代理与测试**

`frontend/vite.config.ts`：
```ts
/// <reference types="vitest" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
  },
})
```

在 `frontend/package.json` 的 `scripts` 中加入：
```json
"test": "vitest run",
"test:watch": "vitest"
```

- [ ] **Step 3: 写设计令牌**

`frontend/src/styles/tokens.css`：
```css
:root {
  --g-bg-1: #0b1020;
  --g-bg-2: #1b2145;
  --g-bg-3: #3a1d5c;

  --g-surface: rgba(255, 255, 255, 0.10);
  --g-surface-strong: rgba(255, 255, 255, 0.16);
  --g-border: rgba(255, 255, 255, 0.22);
  --g-highlight: rgba(255, 255, 255, 0.45);

  --g-text: rgba(255, 255, 255, 0.94);
  --g-text-dim: rgba(255, 255, 255, 0.62);
  --g-accent: #7cc4ff;
  --g-danger: #ff8a8a;
  --g-success: #86e2b0;

  --g-blur: 24px;
  --g-blur-strong: 40px;
  --g-radius: 20px;
  --g-radius-sm: 12px;

  --g-shadow: 0 8px 32px rgba(0, 0, 0, 0.36);
  --g-space: 16px;
}
```

- [ ] **Step 4: 写玻璃基元与全局样式**

`frontend/src/styles/glass.css`：
```css
.glass,
.glass-strong {
  position: relative;
  border: 1px solid var(--g-border);
  border-radius: var(--g-radius);
  box-shadow: var(--g-shadow);
  background: var(--g-surface);
  backdrop-filter: blur(var(--g-blur)) saturate(180%);
  -webkit-backdrop-filter: blur(var(--g-blur)) saturate(180%);
}

.glass-strong {
  background: var(--g-surface-strong);
  backdrop-filter: blur(var(--g-blur-strong)) saturate(200%);
  -webkit-backdrop-filter: blur(var(--g-blur-strong)) saturate(200%);
}

/* 顶边高光，制造玻璃厚度感 */
.glass::before,
.glass-strong::before {
  content: '';
  position: absolute;
  inset: 0 0 auto 0;
  height: 1px;
  border-radius: var(--g-radius) var(--g-radius) 0 0;
  background: linear-gradient(
    90deg,
    transparent,
    var(--g-highlight),
    transparent
  );
  pointer-events: none;
}

.glass-inset {
  border-radius: var(--g-radius-sm);
  background: rgba(0, 0, 0, 0.18);
  box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.4);
}
```

`frontend/src/styles/global.css`：
```css
@import './tokens.css';
@import './glass.css';

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-height: 100dvh;
  color: var(--g-text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Microsoft YaHei', sans-serif;
  background:
    radial-gradient(1200px 800px at 10% 0%, var(--g-bg-3), transparent 60%),
    radial-gradient(1000px 700px at 90% 20%, var(--g-bg-2), transparent 55%),
    var(--g-bg-1);
  background-attachment: fixed;
}
```

- [ ] **Step 5: 写最小 App 验证玻璃效果**

`frontend/src/App.tsx`：
```tsx
import './styles/global.css'

export default function App() {
  return (
    <main style={{ padding: 'var(--g-space)', maxWidth: 960, margin: '0 auto' }}>
      <h1>pptx → PDF</h1>
      <section className="glass" style={{ padding: 24 }}>
        玻璃面板
      </section>
    </main>
  )
}
```

- [ ] **Step 6: 启动确认渲染正常**

Run: `npm run dev`
Expected: `http://localhost:5173` 显示深色渐变背景上的模糊玻璃面板，顶边有高光线

- [ ] **Step 7: 提交**

```bash
git add frontend/
git commit -m "feat(frontend): Vite 脚手架与液态玻璃设计系统"
```

---

### Task 8: 分片切分与退避纯函数

**Files:**
- Create: `frontend/src/lib/chunking.ts`
- Test: `frontend/src/lib/chunking.test.ts`

**Interfaces:**
- Consumes: 无
- Produces:
  - `interface ChunkRange { index: number; start: number; end: number }`
  - `sliceChunks(size: number, chunkSize: number): ChunkRange[]`
  - `backoffDelay(attempt: number): number`
  - `formatBytes(bytes: number): string`
  - `estimateRemaining(bytesSent: number, totalBytes: number, elapsedMs: number): number | null`

- [ ] **Step 1: 写失败测试**

`frontend/src/lib/chunking.test.ts`：
```ts
import { describe, expect, it } from 'vitest'
import {
  backoffDelay,
  estimateRemaining,
  formatBytes,
  sliceChunks,
} from './chunking'

describe('sliceChunks', () => {
  it('splits evenly divisible size', () => {
    expect(sliceChunks(10, 5)).toEqual([
      { index: 0, start: 0, end: 5 },
      { index: 1, start: 5, end: 10 },
    ])
  })

  it('makes the last chunk shorter', () => {
    const chunks = sliceChunks(12, 5)
    expect(chunks).toHaveLength(3)
    expect(chunks[2]).toEqual({ index: 2, start: 10, end: 12 })
  })

  it('handles a file smaller than one chunk', () => {
    expect(sliceChunks(3, 5)).toEqual([{ index: 0, start: 0, end: 3 }])
  })

  it('returns empty for zero size', () => {
    expect(sliceChunks(0, 5)).toEqual([])
  })
})

describe('backoffDelay', () => {
  it('grows exponentially', () => {
    expect(backoffDelay(0)).toBe(500)
    expect(backoffDelay(1)).toBe(1000)
    expect(backoffDelay(2)).toBe(2000)
  })

  it('caps at 10s', () => {
    expect(backoffDelay(20)).toBe(10000)
  })
})

describe('formatBytes', () => {
  it('formats across units', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})

describe('estimateRemaining', () => {
  it('returns null before any progress', () => {
    expect(estimateRemaining(0, 1000, 1000)).toBeNull()
  })

  it('estimates from observed rate', () => {
    // 1000ms 传了 500 字节，剩 500 字节 → 约 1000ms
    expect(estimateRemaining(500, 1000, 1000)).toBe(1000)
  })
})
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test`
Expected: FAIL — 无法解析 `./chunking`

- [ ] **Step 3: 实现**

`frontend/src/lib/chunking.ts`：
```ts
export interface ChunkRange {
  index: number
  start: number
  end: number
}

export function sliceChunks(size: number, chunkSize: number): ChunkRange[] {
  const ranges: ChunkRange[] = []
  for (let start = 0, index = 0; start < size; start += chunkSize, index += 1) {
    ranges.push({ index, start, end: Math.min(start + chunkSize, size) })
  }
  return ranges
}

const BASE_DELAY_MS = 500
const MAX_DELAY_MS = 10_000

export function backoffDelay(attempt: number): number {
  return Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS)
}

const UNITS = ['B', 'KB', 'MB', 'GB'] as const

export function formatBytes(bytes: number): string {
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${UNITS[unit]}`
}

export function estimateRemaining(
  bytesSent: number,
  totalBytes: number,
  elapsedMs: number,
): number | null {
  if (bytesSent <= 0 || elapsedMs <= 0) return null
  const rate = bytesSent / elapsedMs
  return Math.round((totalBytes - bytesSent) / rate)
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npm test`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add frontend/src/lib/
git commit -m "feat(frontend): 分片切分、退避与格式化纯函数"
```

---

### Task 9: 分片上传客户端

**Files:**
- Create: `frontend/src/lib/api.ts`, `frontend/src/lib/uploadClient.ts`
- Test: `frontend/src/lib/uploadClient.test.ts`

**Interfaces:**
- Consumes: `sliceChunks`, `backoffDelay`
- Produces:
  - `api.ts`：`ApiError`, `createUpload`, `putChunk`, `getUploadStatus`, `completeUpload`, `getTask`, `downloadUrl`；类型 `CreateUploadResponse`, `UploadStatusResponse`, `TaskDto`
  - `uploadClient.ts`：`type UploadPhase = 'creating' | 'uploading' | 'completing' | 'done' | 'error'`；`interface UploadProgress { bytesSent: number; totalBytes: number; chunksDone: number; totalChunks: number }`；`uploadFile(file: File, opts?: UploadOptions): Promise<{ taskId: string }>`

- [ ] **Step 1: 写 API 层**

`frontend/src/lib/api.ts`：
```ts
export interface CreateUploadResponse {
  upload_id: string
  chunk_size: number
  total_chunks: number
  expires_at: string
}

export interface UploadStatusResponse {
  received_indices: number[]
  bytes_received: number
  total_chunks: number
  chunk_size: number
  status: string
}

export interface TaskDto {
  task_id: string
  status: 'pending' | 'parsing' | 'queued' | 'converting' | 'done' | 'failed'
  engine: string
  original_filename: string
  size_bytes: number
  slide_count: number | null
  slide_width_emu: number | null
  slide_height_emu: number | null
  fonts: string[]
  error_code: string | null
  error_message: string | null
  created_at: string
}

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

async function parse<T>(resp: Response): Promise<T> {
  if (resp.ok) return (await resp.json()) as T
  let code = 'INTERNAL_ERROR'
  let message = resp.statusText
  try {
    const body = await resp.json()
    code = body.code ?? code
    message = body.message ?? message
  } catch {
    // 响应体不是 JSON，保留状态文本
  }
  throw new ApiError(code, message, resp.status)
}

export async function createUpload(
  filename: string,
  size: number,
): Promise<CreateUploadResponse> {
  const resp = await fetch('/api/uploads', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename, size }),
  })
  return parse<CreateUploadResponse>(resp)
}

export async function putChunk(
  uploadId: string,
  index: number,
  blob: Blob,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`/api/uploads/${uploadId}/chunks/${index}`, {
    method: 'PUT',
    body: blob,
    signal,
  })
  await parse<unknown>(resp)
}

export async function getUploadStatus(
  uploadId: string,
): Promise<UploadStatusResponse> {
  return parse<UploadStatusResponse>(await fetch(`/api/uploads/${uploadId}`))
}

export async function completeUpload(
  uploadId: string,
): Promise<{ task_id: string }> {
  const resp = await fetch(`/api/uploads/${uploadId}/complete`, {
    method: 'POST',
  })
  return parse<{ task_id: string }>(resp)
}

export async function getTask(taskId: string): Promise<TaskDto> {
  return parse<TaskDto>(await fetch(`/api/tasks/${taskId}`))
}

export function downloadUrl(taskId: string): string {
  return `/api/tasks/${taskId}/download`
}
```

- [ ] **Step 2: 写失败测试**

`frontend/src/lib/uploadClient.test.ts`：
```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { uploadFile } from './uploadClient'

const CHUNK = 4

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeFile(size: number): File {
  return new File([new Uint8Array(size)], 'deck.pptx')
}

describe('uploadFile', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('uploads every chunk and returns the task id', async () => {
    const sent: number[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url === '/api/uploads')
          return jsonResponse({
            upload_id: 'u1',
            chunk_size: CHUNK,
            total_chunks: 3,
            expires_at: '2030-01-01T00:00:00Z',
          })
        if (url.includes('/chunks/')) {
          sent.push(Number(url.split('/').pop()))
          return jsonResponse({ index: 0, received_count: sent.length })
        }
        if (url.endsWith('/complete')) return jsonResponse({ task_id: 't1' })
        if (init?.method === undefined)
          return jsonResponse({
            received_indices: [],
            bytes_received: 0,
            total_chunks: 3,
            chunk_size: CHUNK,
            status: 'active',
          })
        return jsonResponse({}, 500)
      }),
    )

    const result = await uploadFile(makeFile(10))

    expect(result.taskId).toBe('t1')
    expect(sent.sort()).toEqual([0, 1, 2])
  })

  it('skips chunks the server already has', async () => {
    const sent: number[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/uploads')
          return jsonResponse({
            upload_id: 'u1',
            chunk_size: CHUNK,
            total_chunks: 3,
            expires_at: '2030-01-01T00:00:00Z',
          })
        if (url === '/api/uploads/u1')
          return jsonResponse({
            received_indices: [0, 1],
            bytes_received: 8,
            total_chunks: 3,
            chunk_size: CHUNK,
            status: 'active',
          })
        if (url.includes('/chunks/')) {
          sent.push(Number(url.split('/').pop()))
          return jsonResponse({ index: 0, received_count: 3 })
        }
        return jsonResponse({ task_id: 't1' })
      }),
    )

    await uploadFile(makeFile(10), { resumeUploadId: 'u1' })

    expect(sent).toEqual([2])
  })

  it('retries a failing chunk then succeeds', async () => {
    let chunkAttempts = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/uploads')
          return jsonResponse({
            upload_id: 'u1',
            chunk_size: CHUNK,
            total_chunks: 1,
            expires_at: '2030-01-01T00:00:00Z',
          })
        if (url.includes('/chunks/')) {
          chunkAttempts += 1
          if (chunkAttempts < 3) return jsonResponse({ code: 'X' }, 500)
          return jsonResponse({ index: 0, received_count: 1 })
        }
        return jsonResponse({ task_id: 't1' })
      }),
    )

    const result = await uploadFile(makeFile(3), { retryBaseMs: 0 })

    expect(chunkAttempts).toBe(3)
    expect(result.taskId).toBe('t1')
  })

  it('reports progress monotonically', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/uploads')
          return jsonResponse({
            upload_id: 'u1',
            chunk_size: CHUNK,
            total_chunks: 3,
            expires_at: '2030-01-01T00:00:00Z',
          })
        if (url.includes('/chunks/'))
          return jsonResponse({ index: 0, received_count: 1 })
        return jsonResponse({ task_id: 't1' })
      }),
    )

    const seen: number[] = []
    await uploadFile(makeFile(10), {
      onProgress: (p) => seen.push(p.bytesSent),
    })

    expect(seen.at(-1)).toBe(10)
    expect([...seen].sort((a, b) => a - b)).toEqual(seen)
  })
})
```

- [ ] **Step 3: 运行确认失败**

Run: `npm test -- uploadClient`
Expected: FAIL — 无法解析 `./uploadClient`

- [ ] **Step 4: 实现上传客户端**

`frontend/src/lib/uploadClient.ts`：
```ts
import {
  completeUpload,
  createUpload,
  getUploadStatus,
  putChunk,
} from './api'
import { backoffDelay, sliceChunks } from './chunking'

export type UploadPhase =
  | 'creating'
  | 'uploading'
  | 'completing'
  | 'done'
  | 'error'

export interface UploadProgress {
  bytesSent: number
  totalBytes: number
  chunksDone: number
  totalChunks: number
}

export interface UploadOptions {
  onProgress?: (p: UploadProgress) => void
  onPhase?: (phase: UploadPhase) => void
  signal?: AbortSignal
  concurrency?: number
  maxRetries?: number
  retryBaseMs?: number
  /** 续传已有会话；省略则新建 */
  resumeUploadId?: string
}

const DEFAULT_CONCURRENCY = 3
const DEFAULT_MAX_RETRIES = 3

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export async function uploadFile(
  file: File,
  opts: UploadOptions = {},
): Promise<{ taskId: string }> {
  const {
    onProgress,
    onPhase,
    signal,
    concurrency = DEFAULT_CONCURRENCY,
    maxRetries = DEFAULT_MAX_RETRIES,
    retryBaseMs,
    resumeUploadId,
  } = opts

  try {
    onPhase?.('creating')

    // 续传时块大小必须取服务端的权威值。不能用 size/total_chunks 反推——
    // 最后一块通常不满，反推会得到错误的切分边界。
    let session: { upload_id: string; chunk_size: number; total_chunks: number }
    let already: Set<number>

    if (resumeUploadId) {
      const status = await getUploadStatus(resumeUploadId)
      session = {
        upload_id: resumeUploadId,
        chunk_size: status.chunk_size,
        total_chunks: status.total_chunks,
      }
      already = new Set(status.received_indices)
    } else {
      session = await createUpload(file.name, file.size)
      already = new Set<number>()
    }

    const all = sliceChunks(file.size, session.chunk_size)
    const pending = all.filter((c) => !already.has(c.index))

    let bytesSent = all
      .filter((c) => already.has(c.index))
      .reduce((sum, c) => sum + (c.end - c.start), 0)
    let chunksDone = already.size

    const emit = () =>
      onProgress?.({
        bytesSent,
        totalBytes: file.size,
        chunksDone,
        totalChunks: all.length,
      })

    onPhase?.('uploading')
    emit()

    let cursor = 0
    const worker = async (): Promise<void> => {
      while (cursor < pending.length) {
        if (signal?.aborted) throw new DOMException('aborted', 'AbortError')
        const chunk = pending[cursor]
        cursor += 1

        for (let attempt = 0; ; attempt += 1) {
          try {
            await putChunk(
              session.upload_id,
              chunk.index,
              file.slice(chunk.start, chunk.end),
              signal,
            )
            break
          } catch (err) {
            if (signal?.aborted || attempt >= maxRetries - 1) throw err
            await sleep(retryBaseMs ?? backoffDelay(attempt))
          }
        }

        bytesSent += chunk.end - chunk.start
        chunksDone += 1
        emit()
      }
    }

    await Promise.all(
      Array.from({ length: Math.min(concurrency, pending.length || 1) }, worker),
    )

    onPhase?.('completing')
    const { task_id } = await completeUpload(session.upload_id)
    onPhase?.('done')
    return { taskId: task_id }
  } catch (err) {
    onPhase?.('error')
    throw err
  }
}
```

- [ ] **Step 5: 运行确认通过**

Run: `npm test`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add frontend/src/lib/
git commit -m "feat(frontend): 并发分片上传客户端，含续传与指数退避"
```

---

### Task 10: 上传界面与任务列表

**Files:**
- Create: `frontend/src/components/UploadDropzone.tsx`, `frontend/src/components/UploadProgress.tsx`, `frontend/src/components/TaskCard.tsx`, `frontend/src/components/TaskList.tsx`
- Create: `frontend/src/hooks/useTaskPolling.ts`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/src/components/UploadDropzone.test.tsx`

**Interfaces:**
- Consumes: `uploadFile`, `UploadProgress`, `UploadPhase`, `getTask`, `downloadUrl`, `TaskDto`, `formatBytes`, `estimateRemaining`
- Produces:
  - `<UploadDropzone onFileSelected={(f: File) => void} maxBytes={number} />`
  - `<UploadProgress progress={UploadProgress} phase={UploadPhase} />`
  - `<TaskCard task={TaskDto} />`
  - `<TaskList taskIds={string[]} />`
  - `useTaskPolling(taskId: string): TaskDto | null`

- [ ] **Step 1: 写 Dropzone 的失败测试**

`frontend/src/components/UploadDropzone.test.tsx`：
```tsx
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { UploadDropzone } from './UploadDropzone'

const MAX = 600 * 1024 * 1024

function fileOfSize(size: number): File {
  const f = new File(['x'], 'deck.pptx')
  Object.defineProperty(f, 'size', { value: size })
  return f
}

describe('UploadDropzone', () => {
  it('accepts a valid pptx', () => {
    const onFileSelected = vi.fn()
    render(<UploadDropzone onFileSelected={onFileSelected} maxBytes={MAX} />)

    const input = screen.getByTestId('file-input')
    fireEvent.change(input, { target: { files: [fileOfSize(1024)] } })

    expect(onFileSelected).toHaveBeenCalledOnce()
  })

  it('rejects a file over the limit and shows a message', () => {
    const onFileSelected = vi.fn()
    render(<UploadDropzone onFileSelected={onFileSelected} maxBytes={MAX} />)

    const input = screen.getByTestId('file-input')
    fireEvent.change(input, { target: { files: [fileOfSize(MAX + 1)] } })

    expect(onFileSelected).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('超过上限')
  })

  it('rejects a non-pptx extension', () => {
    const onFileSelected = vi.fn()
    render(<UploadDropzone onFileSelected={onFileSelected} maxBytes={MAX} />)

    const wrong = new File(['x'], 'notes.pdf')
    fireEvent.change(screen.getByTestId('file-input'), {
      target: { files: [wrong] },
    })

    expect(onFileSelected).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('.pptx')
  })
})
```

在 `frontend/src/setupTests.ts` 中加入并在 `vite.config.ts` 的 `test` 段加 `setupFiles: './src/setupTests.ts'`：
```ts
import '@testing-library/jest-dom/vitest'
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test -- UploadDropzone`
Expected: FAIL — 无法解析 `./UploadDropzone`

- [ ] **Step 3: 实现 UploadDropzone**

`frontend/src/components/UploadDropzone.tsx`：
```tsx
import { useRef, useState } from 'react'
import { formatBytes } from '../lib/chunking'

interface Props {
  onFileSelected: (file: File) => void
  maxBytes: number
}

export function UploadDropzone({ onFileSelected, maxBytes }: Props) {
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const accept = (file: File | undefined) => {
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.pptx')) {
      setError('只接受 .pptx 文件')
      return
    }
    if (file.size > maxBytes) {
      setError(`文件 ${formatBytes(file.size)}，超过上限 ${formatBytes(maxBytes)}`)
      return
    }
    setError(null)
    onFileSelected(file)
  }

  return (
    <div>
      <div
        className={dragging ? 'glass-strong' : 'glass'}
        style={{
          padding: 40,
          textAlign: 'center',
          cursor: 'pointer',
          transition: 'background 200ms ease',
        }}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          accept(e.dataTransfer.files[0])
        }}
      >
        <p style={{ fontSize: 18, margin: 0 }}>拖入 pptx，或点击选择</p>
        <p style={{ color: 'var(--g-text-dim)', fontSize: 14 }}>
          单文件上限 {formatBytes(maxBytes)}
        </p>
        <input
          data-testid="file-input"
          ref={inputRef}
          type="file"
          accept=".pptx"
          hidden
          onChange={(e) => accept(e.target.files?.[0])}
        />
      </div>
      {error && (
        <p role="alert" style={{ color: 'var(--g-danger)', marginTop: 12 }}>
          {error}
        </p>
      )}
    </div>
  )
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npm test -- UploadDropzone`
Expected: 3 passed

- [ ] **Step 5: 实现进度、轮询与任务卡片**

`frontend/src/components/UploadProgress.tsx`：
```tsx
import { useEffect, useRef, useState } from 'react'
import { estimateRemaining, formatBytes } from '../lib/chunking'
import type { UploadPhase, UploadProgress as P } from '../lib/uploadClient'

const PHASE_LABEL: Record<UploadPhase, string> = {
  creating: '创建会话',
  uploading: '上传中',
  completing: '拼装校验',
  done: '上传完成',
  error: '上传失败',
}

export function UploadProgress({
  progress,
  phase,
}: {
  progress: P
  phase: UploadPhase
}) {
  const started = useRef(Date.now())
  const [, tick] = useState(0)

  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 500)
    return () => clearInterval(id)
  }, [])

  const pct = progress.totalBytes
    ? (progress.bytesSent / progress.totalBytes) * 100
    : 0
  const remaining = estimateRemaining(
    progress.bytesSent,
    progress.totalBytes,
    Date.now() - started.current,
  )

  return (
    <div className="glass" style={{ padding: 20, marginTop: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <strong>{PHASE_LABEL[phase]}</strong>
        <span style={{ color: 'var(--g-text-dim)' }}>
          {progress.chunksDone} / {progress.totalChunks} 块
        </span>
      </div>
      <div
        className="glass-inset"
        style={{ height: 10, marginTop: 12, overflow: 'hidden' }}
      >
        <div
          style={{
            width: `${pct}%`,
            height: '100%',
            background: 'var(--g-accent)',
            transition: 'width 200ms ease',
          }}
        />
      </div>
      <p style={{ color: 'var(--g-text-dim)', fontSize: 14, marginBottom: 0 }}>
        {formatBytes(progress.bytesSent)} / {formatBytes(progress.totalBytes)}
        {remaining !== null && phase === 'uploading'
          ? ` · 约剩 ${Math.ceil(remaining / 1000)} 秒`
          : ''}
      </p>
    </div>
  )
}
```

`frontend/src/hooks/useTaskPolling.ts`：
```ts
import { useEffect, useState } from 'react'
import { getTask, type TaskDto } from '../lib/api'

const POLL_MS = 1000
const TERMINAL = new Set(['done', 'failed'])

export function useTaskPolling(taskId: string): TaskDto | null {
  const [task, setTask] = useState<TaskDto | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: number

    const poll = async () => {
      try {
        const next = await getTask(taskId)
        if (cancelled) return
        setTask(next)
        if (!TERMINAL.has(next.status)) {
          timer = window.setTimeout(poll, POLL_MS)
        }
      } catch {
        if (!cancelled) timer = window.setTimeout(poll, POLL_MS * 3)
      }
    }

    void poll()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [taskId])

  return task
}
```

`frontend/src/components/TaskCard.tsx`：
```tsx
import { downloadUrl, type TaskDto } from '../lib/api'
import { formatBytes } from '../lib/chunking'
import { useTaskPolling } from '../hooks/useTaskPolling'

const STATUS_LABEL: Record<TaskDto['status'], string> = {
  pending: '排队中',
  parsing: '解析中',
  queued: '等待转换',
  converting: '转换中',
  done: '完成',
  failed: '失败',
}

const EMU_PER_INCH = 914400

export function TaskCard({ taskId }: { taskId: string }) {
  const task = useTaskPolling(taskId)

  if (!task) {
    return (
      <div className="glass" style={{ padding: 16 }}>
        载入中…
      </div>
    )
  }

  const ratio =
    task.slide_width_emu && task.slide_height_emu
      ? (task.slide_width_emu / EMU_PER_INCH).toFixed(1) +
        '″ × ' +
        (task.slide_height_emu / EMU_PER_INCH).toFixed(1) +
        '″'
      : null

  return (
    <div className="glass" style={{ padding: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
        <strong style={{ wordBreak: 'break-all' }}>
          {task.original_filename}
        </strong>
        <span
          style={{
            color:
              task.status === 'failed'
                ? 'var(--g-danger)'
                : task.status === 'done'
                  ? 'var(--g-success)'
                  : 'var(--g-text-dim)',
            whiteSpace: 'nowrap',
          }}
        >
          {STATUS_LABEL[task.status]}
        </span>
      </div>

      <p style={{ color: 'var(--g-text-dim)', fontSize: 14 }}>
        {formatBytes(task.size_bytes)}
        {task.slide_count !== null ? ` · ${task.slide_count} 页` : ''}
        {ratio ? ` · ${ratio}` : ''}
      </p>

      {task.status === 'failed' && (
        <p role="alert" style={{ color: 'var(--g-danger)', fontSize: 14 }}>
          {task.error_code}：{task.error_message}
        </p>
      )}

      {task.status === 'done' && (
        <a
          className="glass-strong"
          href={downloadUrl(task.task_id)}
          style={{
            display: 'inline-block',
            padding: '8px 20px',
            color: 'var(--g-text)',
            textDecoration: 'none',
          }}
        >
          下载 PDF
        </a>
      )}
    </div>
  )
}
```

`frontend/src/components/TaskList.tsx`：
```tsx
import { TaskCard } from './TaskCard'

export function TaskList({ taskIds }: { taskIds: string[] }) {
  if (taskIds.length === 0) {
    return (
      <p style={{ color: 'var(--g-text-dim)' }}>还没有任务。上传一个 pptx 开始。</p>
    )
  }
  return (
    <div style={{ display: 'grid', gap: 12 }}>
      {taskIds.map((id) => (
        <TaskCard key={id} taskId={id} />
      ))}
    </div>
  )
}
```

- [ ] **Step 6: 接入 App**

`frontend/src/App.tsx`：
```tsx
import { useState } from 'react'
import { TaskList } from './components/TaskList'
import { UploadDropzone } from './components/UploadDropzone'
import { UploadProgress } from './components/UploadProgress'
import {
  uploadFile,
  type UploadPhase,
  type UploadProgress as P,
} from './lib/uploadClient'
import './styles/global.css'

const MAX_BYTES = 600 * 1024 * 1024

export default function App() {
  const [taskIds, setTaskIds] = useState<string[]>([])
  const [progress, setProgress] = useState<P | null>(null)
  const [phase, setPhase] = useState<UploadPhase>('done')
  const [error, setError] = useState<string | null>(null)

  const handleFile = async (file: File) => {
    setError(null)
    try {
      const { taskId } = await uploadFile(file, {
        onProgress: setProgress,
        onPhase: setPhase,
      })
      setTaskIds((prev) => [taskId, ...prev])
      setProgress(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="layout">
      <header>
        <h1>pptx → PDF</h1>
        <p style={{ color: 'var(--g-text-dim)' }}>
          一期：上传骨架已通，转换引擎待接入
        </p>
      </header>

      <section>
        <UploadDropzone onFileSelected={handleFile} maxBytes={MAX_BYTES} />
        {progress && <UploadProgress progress={progress} phase={phase} />}
        {error && (
          <p role="alert" style={{ color: 'var(--g-danger)' }}>
            {error}
          </p>
        )}
      </section>

      <section>
        <h2>任务</h2>
        <TaskList taskIds={taskIds} />
      </section>
    </div>
  )
}
```

- [ ] **Step 7: 运行全部前端测试**

Run: `npm test`
Expected: 全部 passed

- [ ] **Step 8: 提交**

```bash
git add frontend/src/
git commit -m "feat(frontend): 上传界面、进度与任务轮询"
```

---

### Task 11: 三端响应式布局与端到端验证

**Files:**
- Create: `frontend/src/styles/layout.css`
- Modify: `frontend/src/styles/global.css`（追加 `@import './layout.css';`）
- Create: `backend/tests/test_e2e_large_upload.py`
- Create: `deploy/nginx.conf.example`
- Create: `README.md`

**Interfaces:**
- Consumes: 前十个任务的全部产物
- Produces: `.layout` 类的三端断点；可运行的端到端回归测试

- [ ] **Step 1: 写响应式布局**

`frontend/src/styles/layout.css`：
```css
.layout {
  max-width: 1200px;
  margin: 0 auto;
  padding: var(--g-space);
  display: grid;
  gap: 24px;
}

/* 手机：单栏紧凑 */
.layout > header h1 {
  font-size: 24px;
  margin: 8px 0;
}

/* 平板：单栏加宽，留白变大 */
@media (min-width: 768px) {
  .layout {
    padding: 32px;
    gap: 32px;
  }
  .layout > header h1 {
    font-size: 32px;
  }
}

/* 桌面：左上传右任务的双栏 */
@media (min-width: 1024px) {
  .layout {
    grid-template-columns: 1fr 1fr;
    align-items: start;
  }
  .layout > header {
    grid-column: 1 / -1;
  }
}
```

在 `frontend/src/styles/global.css` 的 import 段追加：
```css
@import './layout.css';
```

- [ ] **Step 2: 手动验证三端**

Run: `npm run dev`，用浏览器开发者工具依次切换 375px（手机）、834px（iPad）、1440px（桌面）。
Expected: 375px 单栏、834px 单栏留白加大、1440px 左右双栏；三档下玻璃面板均无横向溢出。

- [ ] **Step 3: 写端到端测试**

`backend/tests/test_e2e_large_upload.py`：
```python
import pytest
from httpx import ASGITransport, AsyncClient
from pptx import Presentation
from pptx.util import Emu
from pypdf import PdfReader

from app.config import settings
from app.db import Base, engine

SLIDES = 120


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "chunk_size", 256 * 1024)
    settings.ensure_dirs()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def big_pptx(tmp_path):
    """120 页 deck —— 正是 Graph 转不了、必须走 LibreOffice 的那一类。"""
    prs = Presentation()
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(6858000)
    for _ in range(SLIDES):
        prs.slides.add_slide(prs.slide_layouts[6])
    path = tmp_path / "big.pptx"
    prs.save(path)
    return path.read_bytes()


async def test_interrupted_upload_resumes_and_completes(client, big_pptx, tmp_path):
    created = (
        await client.post(
            "/api/uploads", json={"filename": "big.pptx", "size": len(big_pptx)}
        )
    ).json()
    uid, size, total = (
        created["upload_id"],
        created["chunk_size"],
        created["total_chunks"],
    )
    assert total > 1, "样本需要多于一块才能验证续传"

    # 只传前半段，模拟中途断线
    half = total // 2
    for idx in range(half):
        await client.put(
            f"/api/uploads/{uid}/chunks/{idx}",
            content=big_pptx[idx * size : (idx + 1) * size],
        )

    status = (await client.get(f"/api/uploads/{uid}")).json()
    assert status["received_indices"] == list(range(half))

    # 续传剩余块
    for idx in range(half, total):
        await client.put(
            f"/api/uploads/{uid}/chunks/{idx}",
            content=big_pptx[idx * size : (idx + 1) * size],
        )

    task_id = (await client.post(f"/api/uploads/{uid}/complete")).json()["task_id"]

    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "done"
    assert task["slide_count"] == SLIDES

    pdf = tmp_path / "out.pdf"
    pdf.write_bytes((await client.get(f"/api/tasks/{task_id}/download")).content)
    assert len(PdfReader(pdf).pages) == SLIDES
```

- [ ] **Step 4: 运行端到端测试**

Run: `.venv/Scripts/python -m pytest tests/test_e2e_large_upload.py -v`
Expected: 1 passed

- [ ] **Step 5: 固化 OpenAPI 契约快照**

`backend/scripts/dump_openapi.py`：
```python
"""导出 OpenAPI 契约快照。

二三期把占位引擎换成 LibreOffice / Graph 时，重跑本脚本后 git diff 应当为空——
若有差异，说明改动破坏了前端依赖的契约。
"""

import json
from pathlib import Path

from app.main import app

OUT = Path(__file__).resolve().parents[1] / "openapi.json"


def main() -> None:
    OUT.write_text(
        json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
```

Run: `.venv/Scripts/python -m scripts.dump_openapi`
Expected: 生成 `backend/openapi.json`，其中包含 `/api/uploads`、`/api/uploads/{upload_id}/chunks/{index}`、`/api/tasks/{task_id}`、`/api/tasks/{task_id}/download` 四组路径

- [ ] **Step 6: 写 Nginx 配置样例**

`deploy/nginx.conf.example`：
```nginx
server {
    listen 443 ssl http2;
    server_name pptx.example.com;

    # 分片上传后单次请求体只有 5 MiB，无需为 500MB 放宽。
    # 留 16m 余量覆盖块加请求头。
    client_max_body_size 16m;
    client_body_timeout 120s;

    # 前端静态资源
    location / {
        root /srv/pptx2pdf/frontend/dist;
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 大文件下载不缓冲，避免占满内存
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

- [ ] **Step 7: 写 README**

`README.md`：
```markdown
# pptx → PDF

把课程 pptx 转成能直接导入 GoodNotes / OneNote 的 PDF。

**当前进度：一期（前端 + 上传骨架）。转换引擎尚未接入，输出为占位 PDF——页数与页面尺寸真实，内容是占位文字。**

## 开发

后端：
```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest
.venv/Scripts/uvicorn app.main:app --reload
```

前端：
```bash
cd frontend
npm install
npm test
npm run dev
```

前端开发服务器在 5173，`/api` 已代理到 8000。

## 分期

| 期 | 内容 | 状态 |
|---|---|---|
| 一 | 前端三端 UI + 分片上传全链路 + 元信息解析 + 占位 PDF | 进行中 |
| 二 | LibreOffice 引擎（主力通道） | 未开始 |
| 三 | Microsoft Graph 引擎（小文件高保真）+ 转换切片合并 | 未开始 |
| 四 | 账号、配额、风控、管理面板 | 未开始 |

设计文档见 `docs/superpowers/specs/`，实施计划见 `docs/superpowers/plans/`。
```

- [ ] **Step 8: 运行全部测试并提交**

Run:
```bash
cd backend && .venv/Scripts/python -m pytest -v
cd ../frontend && npm test
```
Expected: 前后端全部 passed

```bash
git add frontend/src/styles/ backend/tests/test_e2e_large_upload.py backend/scripts/ backend/openapi.json deploy/ README.md
git commit -m "feat: 三端响应式布局、端到端回归、OpenAPI 快照与部署样例"
```

---

## 一期完成判据

全部勾选后一期即可交付：

- [ ] 后端 `pytest` 全绿（约 35 项）
- [ ] 前端 `npm test` 全绿
- [ ] 浏览器实测：拖入一个真实课程 pptx，进度条走完，任务卡片显示正确页数与尺寸，能下载到页数相符的占位 PDF
- [ ] 375 / 834 / 1440 三档宽度下无横向溢出
- [ ] `backend/openapi.json` 已提交，作为二期的契约基线
