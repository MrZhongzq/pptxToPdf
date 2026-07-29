# 三期：Graph 引擎与转换切片 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接入 Microsoft Graph 转换引擎，并用 zipfile 流式切片让超出 Graph 限制的大文件也能走这条高保真通道。

**Architecture:** 超限文件在 `splitting` 阶段被切成多个 ≤80 页 / ≤40MB 的分片 pptx，每片作为独立 RQ job 调 Graph 转换，全部完成后由一个依赖它们的汇总 job 按序合并。切片走 zipfile 层面的 part 复制而非 python-pptx，内存开销与整包大小无关。

**Tech Stack:** Python 3.12 / FastAPI / RQ + Redis / SQLite / httpx / cryptography(Fernet) / pypdf / React 19

## Global Constraints

- **三期不部署。** 质量关口是代码审查加单元测试。Graph 的 HTTP 调用、SharePoint 清理、429 退避**不写测试**，留四期配上 Azure 凭证后真机验证。
- **恢复写测试，但只覆盖不需要 Azure 凭证的纯逻辑**：页范围计算、切片、合并页序、分片协调、凭证加解密。
- **不做静默回退。** 用户显式选了 Graph，超限时明确报错，绝不偷偷改用 LibreOffice。
- 阈值：每片 **≤80 页**、**≤40 MiB**（对 Graph 的 100 页硬限与 50MB 实测失败点留余量）。
- 切片**只服务于 Graph**。LibreOffice 永远不切片。
- **部分失败一律整体失败**：任一分片失败则整个任务 `failed`，不合并部分结果。
- 分片**不做 job 层重试**；429/5xx 的退避在引擎内部 HTTP 层处理。
- 所有 API 错误返回 `{"code": "...", "message": "..."}`，码值取自 `app/errors.py`。
- 后端所有路径操作用 `pathlib.Path`。
- `ChunkStore` 不得访问数据库；`pptx_probe` 不得访问 HTTP；`ConversionEngine` 不得访问数据库。

**回归命令**（每个任务结束都要跑）：
```bash
# 后端，工作目录 backend/
.venv/Scripts/python.exe -m pytest -q
# 前端，工作目录 frontend/
npm test && npm run build
```

一期二期遗留的 50 个后端测试 + 19 个前端测试必须始终通过。

---

### Task 1: 配置项、错误码与分片数据模型

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/errors.py`
- Modify: `backend/app/models.py`
- Modify: `backend/requirements.txt`
- Modify: `.env.example`
- Test: `backend/tests/test_models.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `settings.secret_key: str | None`、`graph_max_pages_per_shard: int`、`graph_max_shard_bytes: int`、`graph_request_timeout_s: int`、`graph_max_retries: int`
  - `ShardTooLarge`(`SHARD_TOO_LARGE`, 422)、`GraphNotConfigured`(`GRAPH_NOT_CONFIGURED`, 503)
  - `Task.shard_total: int | None`
  - `TaskShard` 模型
  - `settings.shards_dir` property

- [ ] **Step 1: 加配置项**

`backend/app/config.py` 的 `Settings` 追加字段（放在 `soffice_bin` 之后、`debug_force_*` 之前）：

```python
    # 三期：Graph 引擎
    secret_key: str | None = None
    """Fernet 主密钥（32 字节 urlsafe base64）。未配置则 Graph 引擎不可用——
    不设默认值兜底，那等于没加密。"""
    graph_max_pages_per_shard: int = 80
    graph_max_shard_bytes: int = 40 * MIB
    graph_request_timeout_s: int = 50
    graph_max_retries: int = 3
```

并追加一个 property（放在 `outputs_dir` 之后）：

```python
    @property
    def shards_dir(self) -> Path:
        return self.storage_root / "shards"
```

`ensure_dirs()` 的元组里加上 `self.shards_dir`。

- [ ] **Step 2: 加错误码**

`backend/app/errors.py` 末尾追加：

```python
class ShardTooLarge(AppError):
    code = "SHARD_TOO_LARGE"
    http_status = 422


class GraphNotConfigured(AppError):
    code = "GRAPH_NOT_CONFIGURED"
    http_status = 503
```

- [ ] **Step 3: 写模型的失败测试**

`backend/tests/test_models.py` 追加：

```python
def test_task_shard_defaults(session):
    from app.models import TaskShard

    task = Task(
        task_id="t-shard",
        upload_id="u1",
        original_filename="deck.pptx",
        size_bytes=1024,
        shard_total=3,
    )
    session.add(task)
    shard = TaskShard(
        shard_id="s1",
        task_id="t-shard",
        index=0,
        page_start=1,
        page_end=40,
    )
    session.add(shard)
    session.commit()

    loaded = session.get(TaskShard, "s1")
    assert loaded.status == "pending"
    assert loaded.output_path is None
    assert loaded.error_code is None
    assert session.get(Task, "t-shard").shard_total == 3


def test_task_shard_total_defaults_none(session):
    task = Task(
        task_id="t-plain",
        upload_id="u1",
        original_filename="deck.pptx",
        size_bytes=1024,
    )
    session.add(task)
    session.commit()
    # 不切片的任务 shard_total 必须是 None，而不是 0——
    # 0 会让「未切片」和「切了 0 片」无法区分。
    assert session.get(Task, "t-plain").shard_total is None
```

- [ ] **Step 4: 跑测试确认失败**

Run（工作目录 `backend/`）：`.venv/Scripts/python.exe -m pytest tests/test_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'TaskShard'`

- [ ] **Step 5: 加模型**

`backend/app/models.py` 的 `Task` 类追加一列（放在 `requested_engine` 之后）：

```python
    # 切片总数。None 表示不切片，走单次转换的原路径。
    # 不存已完成计数：多个分片并发完成时自增同一行在 SQLite 上要么加锁
    # 要么丢更新，而 TaskShard.status 本来就是这件事的唯一真相源。
    shard_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

文件末尾追加新模型：

```python
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
```

- [ ] **Step 6: 加依赖**

`backend/requirements.txt` 追加：
```
httpx==0.28.1
cryptography==44.0.0
```

`httpx` 一期只在 `requirements-dev.txt`（测试用 ASGI 客户端），三期 Graph 引擎要用它发真实请求，属生产路径。把 `requirements-dev.txt` 里那行删掉，避免版本漂移。

- [ ] **Step 7: 更新 .env.example**

在「基础设施」一节后追加：

```bash
# ---- Graph 引擎（三期）----
# Fernet 主密钥，32 字节 urlsafe base64。生成：
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# 未配置则 Graph 引擎不可用。密钥丢失等于凭证全废，要去 Azure 重新生成 client secret。
PPTX2PDF_SECRET_KEY=
# 每片上限。对 Graph 的 100 页硬限与 50MB 实测失败点留余量，不要调高。
PPTX2PDF_GRAPH_MAX_PAGES_PER_SHARD=80
PPTX2PDF_GRAPH_MAX_SHARD_BYTES=41943040
# 单次转换请求超时。Graph 自身有约 45 秒硬超时，这里留 5 秒网络余量。
PPTX2PDF_GRAPH_REQUEST_TIMEOUT_S=50
PPTX2PDF_GRAPH_MAX_RETRIES=3
```

- [ ] **Step 8: 装依赖并跑全量回归**

Run（工作目录 `backend/`）：
```bash
.venv/Scripts/pip.exe install -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q
```
Expected: 52 passed（50 既有 + 2 新增）

- [ ] **Step 9: 提交**

```bash
git add backend/app/config.py backend/app/errors.py backend/app/models.py backend/requirements.txt backend/requirements-dev.txt .env.example backend/tests/test_models.py
git commit -m "feat(backend): 三期配置项、错误码与分片数据模型"
```

---

### Task 2: 凭证加密存储

**Files:**
- Create: `backend/app/services/graph_credentials.py`
- Modify: `backend/app/models.py`
- Test: `backend/tests/test_graph_credentials.py`

**Interfaces:**
- Consumes: `settings.secret_key`、`GraphNotConfigured`
- Produces:
  - `GraphCredentialData`（frozen dataclass）：`tenant_id`、`client_id`、`client_secret`、`site_id`、`drive_path`
  - `load_credentials(session) -> GraphCredentialData`
  - `save_credentials(session, tenant_id, client_id, client_secret, site_id, drive_path) -> None`
  - `GraphCredential` 模型

- [ ] **Step 1: 写失败测试**

`backend/tests/test_graph_credentials.py`：

```python
import pytest

from app.config import settings
from app.errors import GraphNotConfigured
from app.services.graph_credentials import (
    GraphCredentialData,
    load_credentials,
    save_credentials,
)

KEY = "8I3F3CqPwlEsmMDLbEIVSXd8oXlmqkOMWFnDPbNXKvA="


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", KEY)


def test_roundtrip(session, with_key):
    save_credentials(
        session,
        tenant_id="tid",
        client_id="cid",
        client_secret="s3cr3t",
        site_id="sid",
        drive_path="staging",
    )
    got = load_credentials(session)
    assert got == GraphCredentialData(
        tenant_id="tid",
        client_id="cid",
        client_secret="s3cr3t",
        site_id="sid",
        drive_path="staging",
    )


def test_secret_is_not_stored_in_plaintext(session, with_key):
    from app.models import GraphCredential

    save_credentials(
        session,
        tenant_id="tid",
        client_id="cid",
        client_secret="s3cr3t",
        site_id="sid",
        drive_path="staging",
    )
    row = session.get(GraphCredential, 1)
    assert "s3cr3t" not in row.client_secret_encrypted
    assert row.client_secret_encrypted != "s3cr3t"


def test_save_overwrites_single_row(session, with_key):
    from app.models import GraphCredential

    save_credentials(session, tenant_id="a", client_id="a", client_secret="a",
                     site_id="a", drive_path="a")
    save_credentials(session, tenant_id="b", client_id="b", client_secret="b",
                     site_id="b", drive_path="b")
    assert session.query(GraphCredential).count() == 1
    assert load_credentials(session).tenant_id == "b"


def test_load_without_record_raises(session, with_key):
    with pytest.raises(GraphNotConfigured) as exc:
        load_credentials(session)
    assert exc.value.code == "GRAPH_NOT_CONFIGURED"


def test_missing_key_raises(session, monkeypatch):
    monkeypatch.setattr(settings, "secret_key", None)
    with pytest.raises(GraphNotConfigured) as exc:
        load_credentials(session)
    assert "PPTX2PDF_SECRET_KEY" in exc.value.message


def test_corrupted_ciphertext_raises(session, with_key):
    from app.models import GraphCredential

    save_credentials(session, tenant_id="t", client_id="c", client_secret="s",
                     site_id="s", drive_path="d")
    row = session.get(GraphCredential, 1)
    row.client_secret_encrypted = "not-a-valid-fernet-token"
    session.commit()

    # 密文损坏必须报 GraphNotConfigured 而不是让裸 InvalidToken 穿透——
    # 后者不是 AppError，会退化成不带错误码的 500。
    with pytest.raises(GraphNotConfigured):
        load_credentials(session)


def test_wrong_key_raises(session, with_key, monkeypatch):
    save_credentials(session, tenant_id="t", client_id="c", client_secret="s",
                     site_id="s", drive_path="d")
    monkeypatch.setattr(
        settings, "secret_key", "Zt7VQKLBB3sfxaMxxLh6EFRmbUlq7wPCM0hEXeYqQ4Y="
    )
    with pytest.raises(GraphNotConfigured):
        load_credentials(session)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_credentials.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.graph_credentials'`

- [ ] **Step 3: 加模型**

`backend/app/models.py` 末尾追加：

```python
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
```

- [ ] **Step 4: 实现凭证服务**

`backend/app/services/graph_credentials.py`：

```python
import logging
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import GraphNotConfigured
from app.models import GraphCredential

logger = logging.getLogger(__name__)

CREDENTIAL_ROW_ID = 1


@dataclass(frozen=True)
class GraphCredentialData:
    tenant_id: str
    client_id: str
    client_secret: str
    site_id: str
    drive_path: str


def _fernet() -> Fernet:
    """取加密器。密钥缺失或格式非法都归一成 GraphNotConfigured——
    这是「引擎没配好」而不是「服务器内部错误」，用户看到的提示应该
    指向配置动作。"""
    if not settings.secret_key:
        raise GraphNotConfigured(
            "未配置 PPTX2PDF_SECRET_KEY，Graph 引擎不可用。"
            "生成方式见 .env.example。"
        )
    try:
        return Fernet(settings.secret_key.encode())
    except (ValueError, TypeError) as exc:
        raise GraphNotConfigured(
            f"PPTX2PDF_SECRET_KEY 不是合法的 Fernet 密钥: {exc}"
        ) from exc


def load_credentials(session: Session) -> GraphCredentialData:
    fernet = _fernet()
    row = session.get(GraphCredential, CREDENTIAL_ROW_ID)
    if row is None:
        raise GraphNotConfigured(
            "尚未配置 Azure 凭证，请在管理页面填写租户与 SharePoint 站点信息"
        )
    try:
        secret = fernet.decrypt(row.client_secret_encrypted.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        # 密钥换过、密文损坏、或数据是用别的密钥加密的。裸 InvalidToken
        # 不是 AppError，会退化成不带错误码的 500。
        logger.error("Graph 凭证解密失败: %s", exc)
        raise GraphNotConfigured(
            "Graph 凭证无法解密——PPTX2PDF_SECRET_KEY 可能已变更。"
            "请在管理页面重新填写 client secret。"
        ) from exc

    return GraphCredentialData(
        tenant_id=row.tenant_id,
        client_id=row.client_id,
        client_secret=secret,
        site_id=row.site_id,
        drive_path=row.drive_path,
    )


def save_credentials(
    session: Session,
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    site_id: str,
    drive_path: str = "pptx2pdf-staging",
) -> None:
    """写入凭证。单行表，重复调用覆盖同一行。

    三期不会调用它——四期的管理页面才是使用方。现在实现好，
    四期直接用，也让加密逻辑在三期就有测试覆盖。
    """
    fernet = _fernet()
    encrypted = fernet.encrypt(client_secret.encode()).decode()

    row = session.get(GraphCredential, CREDENTIAL_ROW_ID)
    if row is None:
        row = GraphCredential(id=CREDENTIAL_ROW_ID)
        session.add(row)
    row.tenant_id = tenant_id
    row.client_id = client_id
    row.client_secret_encrypted = encrypted
    row.site_id = site_id
    row.drive_path = drive_path
    session.commit()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_credentials.py -q`
Expected: 7 passed

- [ ] **Step 6: 跑全量回归并提交**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 59 passed

```bash
git add backend/app/services/graph_credentials.py backend/app/models.py backend/tests/test_graph_credentials.py
git commit -m "feat(backend): Graph 凭证加密存储"
```

---

### Task 3: zipfile 流式切片

**Files:**
- Create: `backend/app/services/pptx_split.py`
- Test: `backend/tests/test_pptx_split.py`

**Interfaces:**
- Consumes: 无（纯 zipfile 操作）
- Produces:
  - `split_pptx(src: Path, ranges: list[tuple[int, int]], out_dir: Path) -> list[Path]`
    按 1-based 闭区间页范围切分，返回各分片路径（顺序与 `ranges` 一致）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_pptx_split.py`：

```python
import io
import random
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Emu, Inches

from app.services.pptx_probe import probe
from app.services.pptx_split import split_pptx

SLIDES = 8


def _noisy_png(w=700, h=450) -> io.BytesIO:
    """随机噪声图压缩率低，单张几百 KB——用来验证 media 确实被裁掉。"""
    img = Image.new("RGB", (w, h))
    img.putdata(
        [
            (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for _ in range(w * h)
        ]
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


@pytest.fixture
def deck(tmp_path) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
    for i in range(SLIDES):
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = f"PAGE-{i + 1}"
        s.shapes.add_picture(_noisy_png(), Inches(1), Inches(2), width=Inches(4))
    path = tmp_path / "deck.pptx"
    prs.save(path)
    return path


def _media_count(path: Path) -> int:
    with zipfile.ZipFile(path) as z:
        return len([n for n in z.namelist() if n.startswith("ppt/media/")])


def test_split_page_counts(deck, tmp_path):
    out = split_pptx(deck, [(1, 3), (4, 8)], tmp_path / "shards")
    assert len(out) == 2
    assert probe(out[0]).slide_count == 3
    assert probe(out[1]).slide_count == 5


def test_split_drops_unreferenced_media(deck, tmp_path):
    assert _media_count(deck) == SLIDES
    out = split_pptx(deck, [(1, 3), (4, 8)], tmp_path / "shards")
    # 关键断言：只从 sldIdLst 删除而不 drop_rel 的实现会让所有 media
    # 留在包里，这里就会看到 8 而不是 3/5。
    assert _media_count(out[0]) == 3
    assert _media_count(out[1]) == 5


def test_split_reduces_file_size(deck, tmp_path):
    out = split_pptx(deck, [(1, 4), (5, 8)], tmp_path / "shards")
    total = sum(p.stat().st_size for p in out)
    # 两片各占一半 media，合计应明显小于原包的 1.5 倍
    # （共享的 theme/master 会在每片里各留一份，所以不是精确的 1.0 倍）
    assert total < deck.stat().st_size * 1.5


def test_split_preserves_page_order_and_content(deck, tmp_path):
    """切片后每页的标题必须还是原来那一页的标题，顺序不能乱。"""
    out = split_pptx(deck, [(1, 3), (4, 8)], tmp_path / "shards")

    first = Presentation(str(out[0]))
    assert [s.shapes.title.text for s in first.slides] == [
        "PAGE-1",
        "PAGE-2",
        "PAGE-3",
    ]
    second = Presentation(str(out[1]))
    assert [s.shapes.title.text for s in second.slides] == [
        "PAGE-4",
        "PAGE-5",
        "PAGE-6",
        "PAGE-7",
        "PAGE-8",
    ]


def test_split_output_is_valid_pptx(deck, tmp_path):
    """产出的包必须能被 python-pptx 打开、被 pptx_probe 解析。"""
    out = split_pptx(deck, [(2, 5)], tmp_path / "shards")
    meta = probe(out[0])
    assert meta.slide_count == 4
    assert meta.slide_width_emu == 12192000
    prs = Presentation(str(out[0]))
    assert len(prs.slides) == 4


def test_single_page_range(deck, tmp_path):
    out = split_pptx(deck, [(5, 5)], tmp_path / "shards")
    assert probe(out[0]).slide_count == 1
    assert Presentation(str(out[0])).slides[0].shapes.title.text == "PAGE-5"


def test_full_range_is_a_faithful_copy(deck, tmp_path):
    out = split_pptx(deck, [(1, SLIDES)], tmp_path / "shards")
    assert probe(out[0]).slide_count == SLIDES
    assert _media_count(out[0]) == SLIDES


def test_rejects_out_of_bounds_range(deck, tmp_path):
    with pytest.raises(ValueError):
        split_pptx(deck, [(1, SLIDES + 1)], tmp_path / "shards")
    with pytest.raises(ValueError):
        split_pptx(deck, [(0, 3)], tmp_path / "shards")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pptx_split.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.pptx_split'`

- [ ] **Step 3: 实现流式切片**

`backend/app/services/pptx_split.py`：

```python
import logging
import posixpath
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

PRESENTATION = "ppt/presentation.xml"
PKG_RELS = "_rels/.rels"

P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"

SLIDE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)
# 讲稿备注、批注、缩略图不进 PDF，带着只会增加 part 数量与出错面。
DROP_REL_TYPES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail",
}

COPY_CHUNK = 1024 * 1024


def _rels_path(part: str) -> str:
    """ppt/slides/slide1.xml -> ppt/slides/_rels/slide1.xml.rels"""
    d, name = posixpath.split(part)
    return posixpath.join(d, "_rels", name + ".rels") if d else f"_rels/{name}.rels"


def _resolve(base_part: str, target: str) -> str:
    """把 rels 里相对于 base_part 所在目录的 Target 解析成包内绝对路径。"""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _read_rels(zf: zipfile.ZipFile, part: str) -> list[tuple[str, str, str]]:
    """返回 [(rId, type, resolved_target)]，跳过外部链接。"""
    rels_name = _rels_path(part)
    try:
        raw = zf.read(rels_name)
    except KeyError:
        return []
    out = []
    for rel in ET.fromstring(raw):
        if rel.get("TargetMode") == "External":
            continue
        out.append(
            (rel.get("Id"), rel.get("Type"), _resolve(part, rel.get("Target")))
        )
    return out


def _collect(zf: zipfile.ZipFile, part: str, keep: set[str]) -> None:
    """从 part 出发递归收集依赖的所有 part（含它自己的 .rels）。"""
    if part in keep:
        return
    keep.add(part)
    rels_name = _rels_path(part)
    if rels_name in zf.namelist():
        keep.add(rels_name)
    for _rid, rel_type, target in _read_rels(zf, part):
        if rel_type in DROP_REL_TYPES:
            continue
        if target not in keep:
            _collect(zf, target, keep)


def _slide_order(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """返回按 sldIdLst 顺序排列的 [(rId, slide_part_name)]。"""
    root = ET.fromstring(zf.read(PRESENTATION))
    lst = root.find(f"{P_NS}sldIdLst")
    if lst is None:
        raise ValueError("presentation.xml 缺少 sldIdLst")
    rid_to_target = {
        rid: target for rid, _t, target in _read_rels(zf, PRESENTATION)
    }
    order = []
    for sld in lst.findall(f"{P_NS}sldId"):
        rid = sld.get(f"{R_NS}id")
        order.append((rid, rid_to_target[rid]))
    return order


def _rewrite_presentation(raw: bytes, keep_rids: set[str]) -> bytes:
    """只保留 keep_rids 对应的 sldId 条目，其余原样不动。"""
    root = ET.fromstring(raw)
    lst = root.find(f"{P_NS}sldIdLst")
    for sld in list(lst):
        if sld.get(f"{R_NS}id") not in keep_rids:
            lst.remove(sld)
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8")


def _rewrite_rels(raw: bytes, keep_parts: set[str], base_part: str) -> bytes:
    """删掉指向未保留 part 的 Relationship。

    rId 一律不重编号：保留的 slide XML 内部有 r:embed="rId3" 这类引用，
    重编号就要同步改写每个 slide 的正文，那是引入 bug 的捷径。
    """
    root = ET.fromstring(raw)
    for rel in list(root):
        if rel.get("TargetMode") == "External":
            continue
        target = _resolve(base_part, rel.get("Target"))
        if target not in keep_parts:
            root.remove(rel)
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8")


def _rewrite_content_types(raw: bytes, keep_parts: set[str]) -> bytes:
    """删掉未保留 part 的 Override。Default（按扩展名）全部保留。"""
    root = ET.fromstring(raw)
    for node in list(root):
        if node.tag == f"{CT_NS}Override":
            part = node.get("PartName", "").lstrip("/")
            if part not in keep_parts:
                root.remove(node)
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8")


def split_pptx(
    src: Path, ranges: list[tuple[int, int]], out_dir: Path
) -> list[Path]:
    """按 1-based 闭区间页范围把 pptx 切成多份，返回各分片路径。

    逐 part 流式复制，内存开销等于最大单个 part（一张图片），与整包
    大小无关——这是不用 python-pptx 的全部理由（后者把整包读进内存，
    500MB 的 deck 会吃掉 2-3GB）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []

    with zipfile.ZipFile(src) as zin:
        order = _slide_order(zin)
        total = len(order)
        for start, end in ranges:
            if not (1 <= start <= end <= total):
                raise ValueError(
                    f"页范围 ({start}, {end}) 超出 deck 的 1..{total}"
                )

        names = set(zin.namelist())
        pres_rels_name = _rels_path(PRESENTATION)

        for idx, (start, end) in enumerate(ranges):
            kept = order[start - 1 : end]
            keep_rids = {rid for rid, _ in kept}
            keep_parts: set[str] = set()

            # 保留的 slide 及其依赖
            for _rid, part in kept:
                _collect(zin, part, keep_parts)
            # presentation 级依赖里除 slide 之外的部分（master / theme /
            # presProps / viewProps / tableStyles），它们不被 slide 直接引用
            for _rid, rel_type, target in _read_rels(zin, PRESENTATION):
                if rel_type == SLIDE_REL_TYPE or rel_type in DROP_REL_TYPES:
                    continue
                _collect(zin, target, keep_parts)
            # 包级：docProps 等
            _collect(zin, PRESENTATION, keep_parts)
            for _rid, rel_type, target in _read_rels(zin, ""):
                if rel_type in DROP_REL_TYPES:
                    continue
                _collect(zin, target, keep_parts)
            keep_parts.update({PKG_RELS, "[Content_Types].xml", pres_rels_name})

            dest = out_dir / f"{idx:03d}.pptx"
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
                for name in zin.namelist():
                    if name not in keep_parts:
                        continue
                    if name == PRESENTATION:
                        zout.writestr(
                            name, _rewrite_presentation(zin.read(name), keep_rids)
                        )
                    elif name == pres_rels_name:
                        zout.writestr(
                            name,
                            _rewrite_rels(zin.read(name), keep_parts, PRESENTATION),
                        )
                    elif name == "[Content_Types].xml":
                        zout.writestr(
                            name, _rewrite_content_types(zin.read(name), keep_parts)
                        )
                    else:
                        # 流式搬运，不把 part 整个读进内存
                        with zin.open(name) as fh, zout.open(name, "w") as out:
                            shutil.copyfileobj(fh, out, COPY_CHUNK)

            logger.info(
                "shard %d 页 %d-%d part=%d 体积=%.1fMB",
                idx, start, end, len(keep_parts), dest.stat().st_size / 1024 / 1024,
            )
            results.append(dest)

    return results
```

**注意 `_read_rels(zin, "")`**：`_rels_path("")` 会得到 `_rels/.rels`，也就是包级 rels。这不是巧合，是刻意让同一个函数同时处理 part 级和包级 rels。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pptx_split.py -q`
Expected: 8 passed

若 `test_split_drops_unreferenced_media` 失败且 media 数等于原始页数，说明依赖收集把所有 slide 都算进去了——检查 `_rewrite_rels` 是否真的删掉了未保留 slide 的 Relationship。

- [ ] **Step 5: 跑全量回归并提交**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 67 passed

```bash
git add backend/app/services/pptx_split.py backend/tests/test_pptx_split.py
git commit -m "feat(backend): zipfile 流式切片，内存与整包大小无关"
```

---

### Task 4: 分片规划

**Files:**
- Create: `backend/app/services/shard_planner.py`
- Test: `backend/tests/test_shard_planner.py`

**Interfaces:**
- Consumes: `settings.graph_max_pages_per_shard`、`graph_max_shard_bytes`、`ShardTooLarge`
- Produces:
  - `plan_ranges(slide_count: int, size_bytes: int, max_pages: int, max_bytes: int) -> list[tuple[int, int]]`
  - `needs_sharding(slide_count: int, size_bytes: int) -> bool`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_shard_planner.py`：

```python
from app.services.shard_planner import needs_sharding, plan_ranges

MIB = 1024 * 1024


def _covers(ranges, total):
    """页范围必须无缝、无重叠地覆盖 1..total。"""
    assert ranges[0][0] == 1
    assert ranges[-1][1] == total
    for (_, prev_end), (nxt_start, _) in zip(ranges, ranges[1:]):
        assert nxt_start == prev_end + 1
    return True


def test_no_sharding_when_within_limits():
    assert needs_sharding(80, 40 * MIB) is False
    assert needs_sharding(1, 1) is False


def test_sharding_when_pages_exceed():
    assert needs_sharding(81, 1 * MIB) is True


def test_sharding_when_bytes_exceed():
    assert needs_sharding(10, 41 * MIB) is True


def test_single_range_when_within_limits():
    assert plan_ranges(50, 10 * MIB, 80, 40 * MIB) == [(1, 50)]


def test_splits_by_page_limit():
    ranges = plan_ranges(200, 10 * MIB, 80, 40 * MIB)
    assert _covers(ranges, 200)
    assert all(end - start + 1 <= 80 for start, end in ranges)
    assert len(ranges) == 3


def test_splits_by_size_limit():
    # 40 页 400MB -> 每页均 10MB -> 每片最多 4 页
    ranges = plan_ranges(40, 400 * MIB, 80, 40 * MIB)
    assert _covers(ranges, 40)
    assert all(end - start + 1 <= 4 for start, end in ranges)


def test_size_limit_wins_over_page_limit():
    ranges = plan_ranges(100, 500 * MIB, 80, 40 * MIB)
    assert _covers(ranges, 100)
    assert all(end - start + 1 <= 8 for start, end in ranges)


def test_boundary_exactly_at_limits():
    assert plan_ranges(80, 40 * MIB, 80, 40 * MIB) == [(1, 80)]
    ranges = plan_ranges(81, 40 * MIB, 80, 40 * MIB)
    assert _covers(ranges, 81)
    assert len(ranges) == 2


def test_single_page_deck():
    assert plan_ranges(1, 100 * MIB, 80, 40 * MIB) == [(1, 1)]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_shard_planner.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现规划器**

`backend/app/services/shard_planner.py`：

```python
import logging
import math

from app.config import settings

logger = logging.getLogger(__name__)


def needs_sharding(slide_count: int, size_bytes: int) -> bool:
    """超过任一维度就需要切片。"""
    return (
        slide_count > settings.graph_max_pages_per_shard
        or size_bytes > settings.graph_max_shard_bytes
    )


def plan_ranges(
    slide_count: int, size_bytes: int, max_pages: int, max_bytes: int
) -> list[tuple[int, int]]:
    """算出 1-based 闭区间的页范围列表，无缝无重叠覆盖整个 deck。

    体积维度按每页均摊估算。这个估算对媒体分布不均的 deck 不准——有的页
    一张大图、有的页只有标题——所以调用方切完必须实测每片体积，超限的
    再切一轮（见 shard_pipeline）。这里只负责给一个合理的初分。
    """
    if slide_count <= 0:
        raise ValueError(f"slide_count 必须为正，收到 {slide_count}")

    avg_page_bytes = max(1, size_bytes // slide_count)
    by_size = max(1, max_bytes // avg_page_bytes)
    pages_per_shard = max(1, min(max_pages, by_size))

    ranges = []
    start = 1
    while start <= slide_count:
        end = min(start + pages_per_shard - 1, slide_count)
        ranges.append((start, end))
        start = end + 1

    logger.info(
        "分片规划: %d 页 %.1fMB -> %d 片，每片最多 %d 页",
        slide_count, size_bytes / 1024 / 1024, len(ranges), pages_per_shard,
    )
    return ranges
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_shard_planner.py -q`
Expected: 9 passed

- [ ] **Step 5: 跑全量回归并提交**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 76 passed

```bash
git add backend/app/services/shard_planner.py backend/tests/test_shard_planner.py
git commit -m "feat(backend): 分片页范围规划"
```

---

### Task 5: PDF 合并与页序保证

**Files:**
- Create: `backend/app/services/pdf_merge.py`
- Test: `backend/tests/test_pdf_merge.py`

**Interfaces:**
- Consumes: `ConversionFailed`
- Produces: `merge_pdfs(parts: list[Path], dest: Path) -> int`（返回总页数）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_pdf_merge.py`：

```python
import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.errors import ConversionFailed
from app.services.pdf_merge import merge_pdfs


def _make_pdf(path, labels):
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for label in labels:
        pdf.setFont("Helvetica", 40)
        pdf.drawCentredString(300, 400, label)
        pdf.showPage()
    pdf.save()
    return path


def _texts(path):
    return [(p.extract_text() or "").strip() for p in PdfReader(str(path)).pages]


def test_merge_preserves_order(tmp_path):
    """页序是三期最危险的失败模式：顺序错了页数完全正确，任何页数
    校验都抓不到，用户可能翻到考前才发现第 30 页跑到了第 10 页。"""
    a = _make_pdf(tmp_path / "a.pdf", ["P1", "P2", "P3"])
    b = _make_pdf(tmp_path / "b.pdf", ["P4", "P5"])
    c = _make_pdf(tmp_path / "c.pdf", ["P6"])

    dest = tmp_path / "merged.pdf"
    pages = merge_pdfs([a, b, c], dest)

    assert pages == 6
    assert _texts(dest) == ["P1", "P2", "P3", "P4", "P5", "P6"]


def test_merge_respects_given_order_not_filename(tmp_path):
    """合并顺序必须由入参决定，不能依赖文件名排序——分片文件名是
    000/001/002，一旦有人改成按 glob 排序，10 片时会变成 0,1,10,2…"""
    a = _make_pdf(tmp_path / "z_first.pdf", ["P1"])
    b = _make_pdf(tmp_path / "a_second.pdf", ["P2"])
    dest = tmp_path / "merged.pdf"

    merge_pdfs([a, b], dest)
    assert _texts(dest) == ["P1", "P2"]


def test_merge_single_part(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["ONLY"])
    dest = tmp_path / "merged.pdf"
    assert merge_pdfs([a], dest) == 1
    assert _texts(dest) == ["ONLY"]


def test_missing_part_raises(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    with pytest.raises(ConversionFailed) as exc:
        merge_pdfs([a, tmp_path / "nope.pdf"], tmp_path / "merged.pdf")
    assert exc.value.code == "CONVERSION_FAILED"


def test_corrupt_part_raises(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(ConversionFailed):
        merge_pdfs([a, bad], tmp_path / "merged.pdf")


def test_empty_parts_raises(tmp_path):
    with pytest.raises(ConversionFailed):
        merge_pdfs([], tmp_path / "merged.pdf")


def test_failed_merge_leaves_no_partial_output(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    dest = tmp_path / "merged.pdf"
    with pytest.raises(ConversionFailed):
        merge_pdfs([a, bad], dest)
    # 半个合并结果比没有更糟——后续的页数校验会拿它当有效产物
    assert not dest.exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pdf_merge.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现合并**

`backend/app/services/pdf_merge.py`：

```python
import logging
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from app.errors import ConversionFailed

logger = logging.getLogger(__name__)


def merge_pdfs(parts: list[Path], dest: Path) -> int:
    """按 parts 的给定顺序合并，返回总页数。

    顺序由入参决定，不做任何排序——调用方按 TaskShard.index 升序传入。
    在这里加 sorted() 会引入按文件名排序的隐患：分片文件名是 000/001/…，
    10 片以上时字典序仍然正确，但任何改名都可能悄悄打乱页序，
    而页序错乱是页数校验抓不到的。
    """
    if not parts:
        raise ConversionFailed("没有可合并的分片")

    writer = PdfWriter()
    try:
        for part in parts:
            if not part.is_file():
                raise ConversionFailed(f"分片结果缺失: {part.name}")
            try:
                reader = PdfReader(str(part))
                for page in reader.pages:
                    writer.add_page(page)
            except ConversionFailed:
                raise
            except Exception as exc:
                raise ConversionFailed(
                    f"分片 {part.name} 无法解析: {exc}"
                ) from exc

        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            writer.write(fh)
    except Exception:
        # 半个合并结果比没有更糟：后续的页数校验会拿它当有效产物。
        dest.unlink(missing_ok=True)
        raise

    total = len(writer.pages)
    logger.info(
        "merged %d 片 -> %d 页 %.1fMB",
        len(parts), total, dest.stat().st_size / 1024 / 1024,
    )
    return total
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pdf_merge.py -q`
Expected: 7 passed

- [ ] **Step 5: 跑全量回归并提交**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 83 passed

```bash
git add backend/app/services/pdf_merge.py backend/tests/test_pdf_merge.py
git commit -m "feat(backend): PDF 合并，页序由入参决定"
```

---

### Task 6: Graph 引擎

**Files:**
- Modify: `backend/app/services/engines/graph.py`（整体替换二期的桩实现）

**Interfaces:**
- Consumes: `load_credentials`、`settings.graph_*`、`ConversionFailed`、`ConversionTimeout`、`EngineUnavailable`、`GraphNotConfigured`
- Produces: `GraphEngine`（`name = "graph"`），实现 `convert(src, meta, dest, *, timeout_s)`

**本任务不写测试**（Global Constraints）：所有代码路径都要真实 Azure 凭证才能执行，mock 出来的 HTTP 往返只会测到 mock 本身。留四期真机验证。

- [ ] **Step 1: 整体替换 graph.py**

```python
import logging
import time
from pathlib import Path

import httpx

from app.config import settings
from app.db import SessionLocal
from app.errors import (
    ConversionFailed,
    ConversionTimeout,
    EngineUnavailable,
)
from app.services.engines.base import ConversionEngine
from app.services.graph_credentials import load_credentials
from app.services.pptx_probe import PptxMeta

logger = logging.getLogger(__name__)

LOGIN_HOST = "https://login.microsoftonline.com"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
UPLOAD_CHUNK = 8 * 1024 * 1024
"""createUploadSession 的分块大小，必须是 320 KiB 的整数倍（Graph 的要求）。"""
TOKEN_SKEW_S = 300


class GraphEngine(ConversionEngine):
    """Microsoft Graph 转换引擎。

    流程：拿 token -> 上传分片到 SharePoint 中转库 -> ?format=pdf 转换
    -> 下载 -> 清理中转文件。

    每次转换新建实例（get_engine 的惰性构造），token 缓存只在单次转换内
    有效。切片场景下每个分片是独立 RQ job、跑在 fork 的子进程里，12 片
    就是 12 次 token 请求——token 端点响应在百毫秒级，相比每片几十秒的
    转换可以忽略，而共享 token 要引入 Redis 级缓存，等于为省几次快请求
    新增一个密钥存储面。不做。
    """

    name = "graph"

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ---- 认证 ----

    def _access_token(self, client: httpx.Client, tenant_id: str,
                      client_id: str, client_secret: str) -> str:
        if self._token and time.time() < self._token_expires_at - TOKEN_SKEW_S:
            return self._token

        resp = client.post(
            f"{LOGIN_HOST}/{tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        if resp.status_code != 200:
            raise EngineUnavailable(
                f"Azure 认证失败（HTTP {resp.status_code}）："
                f"{resp.text[:200]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    # ---- 重试 ----

    def _request_with_retry(
        self, client: httpx.Client, method: str, url: str, **kwargs
    ) -> httpx.Response:
        """429 与 5xx 退避重试；4xx（除 429）立即失败——那是请求本身的
        问题，重试无意义。"""
        last: httpx.Response | None = None
        for attempt in range(settings.graph_max_retries):
            resp = client.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                logger.warning(
                    "Graph 限流，%.0f 秒后重试（第 %d 次）", wait, attempt + 1
                )
                time.sleep(wait)
                last = resp
                continue
            if 500 <= resp.status_code < 600:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "Graph %d，%d 秒后重试（第 %d 次）",
                    resp.status_code, wait, attempt + 1,
                )
                time.sleep(wait)
                last = resp
                continue
            return resp
        raise ConversionFailed(
            f"Graph 请求重试 {settings.graph_max_retries} 次后仍失败："
            f"HTTP {last.status_code if last else '?'}"
        )

    # ---- 上传 ----

    def _upload(self, client: httpx.Client, headers: dict, site_id: str,
                drive_path: str, src: Path) -> str:
        """用 createUploadSession 分片上传，返回 driveItem id。

        小于 4MB 的文件可以直接 PUT content，但分片是 ≤40MB，一律走
        upload session 省掉一条分支。
        """
        target = f"{drive_path}/{src.name}"
        resp = self._request_with_retry(
            client, "POST",
            f"{GRAPH_ROOT}/sites/{site_id}/drive/root:/{target}:/createUploadSession",
            headers=headers,
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        if resp.status_code not in (200, 201):
            raise ConversionFailed(
                f"创建上传会话失败（HTTP {resp.status_code}）：{resp.text[:200]}"
            )
        upload_url = resp.json()["uploadUrl"]

        size = src.stat().st_size
        with src.open("rb") as fh:
            offset = 0
            while offset < size:
                block = fh.read(UPLOAD_CHUNK)
                end = offset + len(block) - 1
                put = client.put(
                    upload_url,
                    content=block,
                    headers={
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                        "Content-Length": str(len(block)),
                    },
                )
                if put.status_code not in (200, 201, 202):
                    raise ConversionFailed(
                        f"上传分块 {offset}-{end} 失败"
                        f"（HTTP {put.status_code}）：{put.text[:200]}"
                    )
                offset = end + 1
                if put.status_code in (200, 201):
                    return put.json()["id"]
        raise ConversionFailed("上传完成但未返回 driveItem id")

    # ---- 清理 ----

    def _cleanup(self, client: httpx.Client, headers: dict, site_id: str,
                 item_id: str) -> None:
        """先试 permanentDelete，403 则退回普通 DELETE 并告警。

        permanentDelete 文档标注的应用权限是 Files.ReadWrite.All /
        Sites.ReadWrite.All，与设计里 Sites.Selected 的最小权限意图冲突；
        社区另有报告称它有时仍把文件送进回收站。三期无法实测，四期配上
        凭证后按实际返回收敛。

        清理失败不影响转换结果——文件已经转好了，中转残留是运维问题。
        但必须记日志：两级回收站仍占 SharePoint 配额，悄悄吃满很难查。
        """
        base = f"{GRAPH_ROOT}/sites/{site_id}/drive/items/{item_id}"
        try:
            resp = client.post(f"{base}/permanentDelete", headers=headers)
            if resp.status_code in (200, 204):
                return
            if resp.status_code == 403:
                logger.warning(
                    "permanentDelete 权限不足（403），退回普通 DELETE。"
                    "中转文件会进回收站并占用配额，考虑改授 Sites.ReadWrite.All"
                )
            else:
                logger.warning(
                    "permanentDelete 返回 %d，退回普通 DELETE", resp.status_code
                )
            fallback = client.delete(base, headers=headers)
            if fallback.status_code not in (200, 204):
                logger.error(
                    "中转文件清理失败 item=%s HTTP %d",
                    item_id, fallback.status_code,
                )
        except httpx.HTTPError as exc:
            logger.error("中转文件清理异常 item=%s: %s", item_id, exc)

    # ---- 主流程 ----

    def convert(
        self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float
    ) -> None:
        session = SessionLocal()
        try:
            creds = load_credentials(session)
        finally:
            session.close()

        dest.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        item_id: str | None = None

        with httpx.Client(
            timeout=settings.graph_request_timeout_s, follow_redirects=True
        ) as client:
            token = self._access_token(
                client, creds.tenant_id, creds.client_id, creds.client_secret
            )
            headers = {"Authorization": f"Bearer {token}"}

            try:
                logger.info(
                    "graph convert start src=%s slides=%d size=%.1fMB",
                    src.name, meta.slide_count, src.stat().st_size / 1024 / 1024,
                )
                item_id = self._upload(
                    client, headers, creds.site_id, creds.drive_path, src
                )

                try:
                    # follow_redirects=True：?format=pdf 返回 302，Location 是
                    # 几分钟内有效的预授权 URL。不跟随就只能拿到空响应体。
                    resp = self._request_with_retry(
                        client, "GET",
                        f"{GRAPH_ROOT}/sites/{creds.site_id}/drive/items/"
                        f"{item_id}/content?format=pdf",
                        headers=headers,
                    )
                except httpx.TimeoutException as exc:
                    raise ConversionTimeout(
                        f"Graph 转换超时（{settings.graph_request_timeout_s} 秒）。"
                        f"Graph 自身有约 45 秒硬超时，该文件可能过于复杂"
                    ) from exc

                if resp.status_code != 200:
                    raise ConversionFailed(
                        f"Graph 转换失败（HTTP {resp.status_code}）："
                        f"{resp.text[:300]}"
                    )
                dest.write_bytes(resp.content)
            finally:
                if item_id:
                    self._cleanup(client, headers, creds.site_id, item_id)

        if not dest.is_file() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise ConversionFailed("Graph 返回了空的 PDF")

        logger.info(
            "graph convert done src=%s elapsed=%.1fs output=%.1fMB",
            src.name, time.monotonic() - started,
            dest.stat().st_size / 1024 / 1024,
        )
```

- [ ] **Step 2: 跑全量回归**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 83 passed

引擎只在被调用时才发请求，import 无副作用，既有测试不受影响。二期那个「选 graph 得到 EngineUnavailable」的行为变了——现在会走到 `load_credentials`，无凭证时抛 `GraphNotConfigured`。如果有测试断言了旧的 `ENGINE_UNAVAILABLE`，改成 `GRAPH_NOT_CONFIGURED` 是正确的适配。

- [ ] **Step 3: 提交**

```bash
git add backend/app/services/engines/graph.py
git commit -m "feat(backend): Graph 引擎实现，替换二期的桩"
```

---

### Task 7: 分片流水线与队列协调

**Files:**
- Create: `backend/app/services/shard_pipeline.py`
- Modify: `backend/app/queue.py`
- Test: `backend/tests/test_shard_pipeline.py`

**Interfaces:**
- Consumes: `split_pptx`、`plan_ranges`、`merge_pdfs`、`get_engine`、`TaskShard`、`settings.shards_dir`
- Produces:
  - `convert_shard(shard_id: str) -> None`
  - `merge_shards(task_id: str) -> None`
  - `enqueue_shards(task_id: str, shard_ids: list[str]) -> None`
  - `shard_dir(task_id: str) -> Path`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_shard_pipeline.py`：

```python
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.config import settings
from app.models import Task, TaskShard
from app.services.shard_pipeline import merge_shards, shard_dir


def _pdf(path, labels):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter)
    for label in labels:
        c.setFont("Helvetica", 40)
        c.drawCentredString(300, 400, label)
        c.showPage()
    c.save()
    return path


@pytest.fixture
def sharded_task(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    settings.ensure_dirs()

    task = Task(
        task_id="T1",
        upload_id="U1",
        original_filename="deck.pptx",
        size_bytes=100,
        slide_count=3,
        engine="graph",
        status="converting",
        shard_total=2,
    )
    session.add(task)
    for i, (ps, pe) in enumerate([(1, 2), (3, 3)]):
        session.add(
            TaskShard(
                shard_id=f"S{i}", task_id="T1", index=i,
                page_start=ps, page_end=pe, status="pending",
            )
        )
    session.commit()
    return task


def test_merge_marks_done_when_all_shards_succeed(session, sharded_task, monkeypatch):
    d = shard_dir("T1")
    for i, labels in enumerate([["P1", "P2"], ["P3"]]):
        p = _pdf(d / f"{i:03d}.pdf", labels)
        shard = session.get(TaskShard, f"S{i}")
        shard.status = "done"
        shard.output_path = str(p)
    session.commit()

    monkeypatch.setattr("app.services.shard_pipeline.SessionLocal", lambda: session)
    merge_shards("T1")

    task = session.get(Task, "T1")
    assert task.status == "done"
    assert task.output_path is not None


def test_merge_fails_when_any_shard_failed(session, sharded_task, monkeypatch):
    """9 成 1 败也必须整体失败——合并出一份缺了中间几页的 PDF，
    比明确报错糟糕得多。"""
    d = shard_dir("T1")
    p = _pdf(d / "000.pdf", ["P1", "P2"])
    s0 = session.get(TaskShard, "S0")
    s0.status, s0.output_path = "done", str(p)
    s1 = session.get(TaskShard, "S1")
    s1.status, s1.error_code, s1.error_message = (
        "failed", "CONVERSION_TIMEOUT", "转换超时",
    )
    session.commit()

    monkeypatch.setattr("app.services.shard_pipeline.SessionLocal", lambda: session)
    merge_shards("T1")

    task = session.get(Task, "T1")
    assert task.status == "failed"
    assert task.error_code == "CONVERSION_TIMEOUT"
    assert task.output_path is None


def test_merge_fails_when_page_count_mismatches(session, sharded_task, monkeypatch):
    """合并结果的总页数必须等于 slide_count。"""
    d = shard_dir("T1")
    for i, labels in enumerate([["P1"], ["P3"]]):  # 少了一页
        p = _pdf(d / f"{i:03d}.pdf", labels)
        shard = session.get(TaskShard, f"S{i}")
        shard.status, shard.output_path = "done", str(p)
    session.commit()

    monkeypatch.setattr("app.services.shard_pipeline.SessionLocal", lambda: session)
    merge_shards("T1")

    task = session.get(Task, "T1")
    assert task.status == "failed"
    assert task.error_code == "CONVERSION_PAGE_MISMATCH"


def test_merge_cleans_shard_dir(session, sharded_task, monkeypatch):
    d = shard_dir("T1")
    for i, labels in enumerate([["P1", "P2"], ["P3"]]):
        p = _pdf(d / f"{i:03d}.pdf", labels)
        shard = session.get(TaskShard, f"S{i}")
        shard.status, shard.output_path = "done", str(p)
    session.commit()

    monkeypatch.setattr("app.services.shard_pipeline.SessionLocal", lambda: session)
    merge_shards("T1")
    # 中间产物体积是原文件两倍（分片 pptx + 分片 PDF），必须清掉
    assert not d.exists()


def test_merge_cleans_shard_dir_on_failure(session, sharded_task, monkeypatch):
    d = shard_dir("T1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "000.pptx").write_bytes(b"leftover")
    s1 = session.get(TaskShard, "S1")
    s1.status, s1.error_code = "failed", "CONVERSION_FAILED"
    session.commit()

    monkeypatch.setattr("app.services.shard_pipeline.SessionLocal", lambda: session)
    merge_shards("T1")
    assert not d.exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_shard_pipeline.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现分片流水线**

`backend/app/services/shard_pipeline.py`：

```python
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.errors import AppError, ConversionPageMismatch
from app.models import Task, TaskShard
from app.services.engines import get_engine
from app.services.pdf_merge import merge_pdfs
from app.services.pptx_probe import probe

logger = logging.getLogger(__name__)


def shard_dir(task_id: str) -> Path:
    return settings.shards_dir / task_id


def convert_shard(shard_id: str) -> None:
    """转换单个分片。只写自己那行 TaskShard，不碰主任务状态——
    多个分片并发运行时不能有对同一行的写竞争。"""
    session = SessionLocal()
    try:
        shard = session.get(TaskShard, shard_id)
        if shard is None:
            logger.warning("convert_shard 收到不存在的 shard_id=%s", shard_id)
            return
        task = session.get(Task, shard.task_id)
        if task is None:
            logger.warning("分片 %s 的主任务已不存在", shard_id)
            return

        src = shard_dir(shard.task_id) / f"{shard.index:03d}.pptx"
        dest = shard_dir(shard.task_id) / f"{shard.index:03d}.pdf"

        shard.status = "converting"
        session.commit()

        try:
            meta = probe(src)
            timeout = float(settings.graph_request_timeout_s)
            get_engine(task.engine).convert(src, meta, dest, timeout_s=timeout)
            shard.output_path = str(dest.resolve())
            shard.status = "done"
            session.commit()
            logger.info(
                "shard done id=%s 页 %d-%d",
                shard_id, shard.page_start, shard.page_end,
            )
        except AppError as exc:
            logger.warning("shard failed id=%s code=%s", shard_id, exc.code)
            _record_shard_failure(session, shard_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001  后台任务兜底
            logger.exception("shard crashed id=%s", shard_id)
            _record_shard_failure(session, shard_id, "INTERNAL_ERROR", str(exc))
    finally:
        session.close()


def _record_shard_failure(session, shard_id: str, code: str, message: str) -> None:
    """与 pipeline._record_failure 同款自保护：落库本身失败时回滚重取，
    仍失败只记日志，绝不再抛——否则 RQ 会把它当作 job 崩溃，而汇总 job
    看到的分片状态还停在 converting。"""
    try:
        session.rollback()
        shard = session.get(TaskShard, shard_id)
        if shard is not None:
            shard.status = "failed"
            shard.error_code = code
            shard.error_message = message
            session.commit()
    except Exception:
        logger.exception("无法记录分片 %s 的失败状态", shard_id)


def merge_shards(task_id: str) -> None:
    """汇总：检查所有分片，全成功则按序合并，任一失败则整体失败。

    由 RQ 的 Dependency(allow_failure=True) 触发——默认的 allow_failure=False
    会让任一分片失败时本 job 永远停在 DeferredJobRegistry 里，任务卡死在
    converting，只能等孤儿回收器 45 分钟后收尸。
    """
    session = SessionLocal()
    started = datetime.now(timezone.utc)
    try:
        task = session.get(Task, task_id)
        if task is None:
            logger.warning("merge_shards 收到不存在的 task_id=%s", task_id)
            return

        shards = (
            session.query(TaskShard)
            .filter(TaskShard.task_id == task_id)
            .order_by(TaskShard.index)
            .all()
        )
        failed = [s for s in shards if s.status == "failed"]
        unfinished = [s for s in shards if s.status not in ("done", "failed")]

        if failed or unfinished:
            first = failed[0] if failed else None
            code = first.error_code if first else "CONVERSION_FAILED"
            msg = (
                f"分片 {first.index} (页 {first.page_start}-{first.page_end}) "
                f"失败：{first.error_message}"
                if first
                else f"{len(unfinished)} 个分片未完成"
            )
            logger.warning("task failed id=%s %s", task_id, msg)
            _fail_task(session, task_id, code or "CONVERSION_FAILED", msg)
            return

        task.status = "merging"
        session.commit()

        try:
            dest = settings.outputs_dir / f"{task_id}.pdf"
            parts = [Path(s.output_path) for s in shards]
            pages = merge_pdfs(parts, dest)

            if task.slide_count is not None and pages != task.slide_count:
                dest.unlink(missing_ok=True)
                raise ConversionPageMismatch(
                    f"合并后页数不符：期望 {task.slide_count} 页，"
                    f"实际 {pages} 页"
                )

            task.output_path = str(dest.resolve())
            task.status = "done"
            session.commit()
            logger.info(
                "task done id=%s shards=%d pages=%d elapsed=%.1fs",
                task_id, len(shards), pages,
                (datetime.now(timezone.utc) - started).total_seconds(),
            )
        except AppError as exc:
            _fail_task(session, task_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("merge crashed id=%s", task_id)
            _fail_task(session, task_id, "INTERNAL_ERROR", str(exc))
    finally:
        # 中间产物体积是原文件的两倍（分片 pptx + 分片 PDF），
        # 不论成败都要清，否则一次 500MB 转换留下 1GB 残骸。
        shutil.rmtree(shard_dir(task_id), ignore_errors=True)
        session.close()


def _fail_task(session, task_id: str, code: str, message: str) -> None:
    try:
        session.rollback()
        task = session.get(Task, task_id)
        if task is not None:
            task.status = "failed"
            task.error_code = code
            task.error_message = message
            session.commit()
    except Exception:
        logger.exception("无法记录任务 %s 的失败状态", task_id)
```

- [ ] **Step 4: 队列侧加分片入队**

`backend/app/queue.py` 追加：

```python
def enqueue_shards(task_id: str, shard_ids: list[str]) -> None:
    """把每个分片作为独立 job 入队，再入队一个依赖它们全部的汇总 job。

    allow_failure=True 是必须的：默认的 False 会让任一分片失败时汇总 job
    永远停在 DeferredJobRegistry 里不执行，任务卡死在 converting。
    """
    from rq.job import Dependency

    from app.services.shard_pipeline import convert_shard, merge_shards

    q = get_queue()
    job_timeout = settings.convert_timeout_max_s + JOB_TIMEOUT_MARGIN_S
    shard_jobs = [
        q.enqueue(convert_shard, sid, job_timeout=job_timeout) for sid in shard_ids
    ]
    q.enqueue(
        merge_shards,
        task_id,
        depends_on=Dependency(jobs=shard_jobs, allow_failure=True),
        job_timeout=job_timeout,
    )
    logger.info("enqueued %d shards + merge for task=%s", len(shard_ids), task_id)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_shard_pipeline.py -q`
Expected: 5 passed

- [ ] **Step 6: 跑全量回归并提交**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 88 passed

```bash
git add backend/app/services/shard_pipeline.py backend/app/queue.py backend/tests/test_shard_pipeline.py
git commit -m "feat(backend): 分片转换与汇总合并，RQ Dependency 协调"
```

---

### Task 8: 主流水线接线与引擎路由

**Files:**
- Modify: `backend/app/services/pipeline.py`
- Modify: `backend/app/services/engine_router.py`
- Test: `backend/tests/test_pipeline_sharding.py`

**Interfaces:**
- Consumes: `needs_sharding`、`plan_ranges`、`split_pptx`、`enqueue_shards`、`ShardTooLarge`
- Produces: `run_task` 在需要时进入 `splitting` 分支并派发分片

- [ ] **Step 1: 写失败测试**

`backend/tests/test_pipeline_sharding.py`：

```python
import pytest

from app.config import settings
from app.services.engine_router import select_engine
from app.services.pptx_probe import PptxMeta

MIB = 1024 * 1024


def _meta(pages: int) -> PptxMeta:
    return PptxMeta(
        slide_count=pages,
        slide_width_emu=12192000,
        slide_height_emu=6858000,
        fonts=(),
    )


def test_auto_routes_small_file_to_graph():
    assert select_engine(_meta(50), 10 * MIB) == "graph"


def test_auto_routes_long_deck_to_libreoffice():
    """自动判定不选切片路径：切片意味着数十次 HTTP 往返与几分钟等待，
    作为默认行为太重。"""
    assert select_engine(_meta(200), 10 * MIB) == "libreoffice"


def test_auto_routes_large_file_to_libreoffice():
    assert select_engine(_meta(50), 100 * MIB) == "libreoffice"


def test_explicit_request_always_wins():
    assert select_engine(_meta(500), 500 * MIB, requested="graph") == "graph"
    assert select_engine(_meta(5), 1 * MIB, requested="libreoffice") == "libreoffice"


def test_boundary_exactly_at_limits():
    assert select_engine(_meta(80), 40 * MIB) == "graph"
    assert select_engine(_meta(81), 40 * MIB) == "libreoffice"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline_sharding.py -q`
Expected: FAIL — `select_engine` 恒返回 `"libreoffice"`，前两个断言不成立

- [ ] **Step 3: 让自动判定分叉生效**

`backend/app/services/engine_router.py` 的 `select_engine` 函数体替换为：

```python
    if requested:
        logger.info("engine 由用户指定: %s", requested)
        return requested
    # 自动判定不选切片路径：切片意味着数十次 HTTP 往返与几分钟等待，
    # 作为默认行为太重。用户显式选 Graph 且文件超限时才切片——那是
    # 他知情的选择。
    if (
        meta.slide_count <= settings.graph_max_pages_per_shard
        and size_bytes <= settings.graph_max_shard_bytes
    ):
        return "graph"
    return DEFAULT_ENGINE
```

文件顶部加 `from app.config import settings`。

- [ ] **Step 4: 在 run_task 里接入切片分支**

`backend/app/services/pipeline.py` 的 `run_task`，把 `_set_status(session, task, "queued")` 到 `convert(...)` 那一段替换为：

```python
            _set_status(session, task, "queued")

            if task.engine == "graph" and needs_sharding(meta.slide_count, size_bytes):
                _dispatch_shards(session, task, src, size_bytes)
                return  # 后续由 convert_shard / merge_shards 接手

            _set_status(session, task, "converting")
            timeout_s = compute_timeout_s(meta.slide_count, size_bytes)
            get_engine(task.engine).convert(src, meta, dest, timeout_s=timeout_s)
```

并在模块内加入切片派发函数：

```python
def _dispatch_shards(session, task: Task, src: Path, size_bytes: int) -> None:
    """切片、落 TaskShard、派发 job。切完实测每片体积，超限的再切一轮。

    抛异常时必须自己清掉已写出的分片文件：派发失败意味着 merge_shards
    永远不会执行，而它的 finally 是分片目录的正常清理路径。不清的话
    一个 500MB 文件会留下约 1GB 残骸，只能等 24 小时后的保留策略。
    """
    try:
        _dispatch_shards_inner(session, task, src, size_bytes)
    except Exception:
        shutil.rmtree(shard_dir(task.task_id), ignore_errors=True)
        raise


def _dispatch_shards_inner(session, task: Task, src: Path, size_bytes: int) -> None:
    _set_status(session, task, "splitting")

    ranges = plan_ranges(
        task.slide_count,
        size_bytes,
        settings.graph_max_pages_per_shard,
        settings.graph_max_shard_bytes,
    )
    out_dir = shard_dir(task.task_id)
    shutil.rmtree(out_dir, ignore_errors=True)
    parts = split_pptx(src, ranges, out_dir)

    # 估算按每页均摊，对媒体分布不均的 deck 不准。实测超限的再切一轮，
    # 最多一轮——递归下去必然收敛到「单页仍超限」这个无解状态，
    # 只是多绕几圈、多写几十 MB 临时文件。
    oversized = [
        (i, p) for i, p in enumerate(parts)
        if p.stat().st_size > settings.graph_max_shard_bytes
    ]
    if oversized:
        logger.warning("%d 片超限，重新规划后再切一轮", len(oversized))
        finer: list[tuple[int, int]] = []
        for i, part in enumerate(parts):
            start, end = ranges[i]
            if part.stat().st_size <= settings.graph_max_shard_bytes:
                finer.append((start, end))
                continue
            pages = end - start + 1
            if pages == 1:
                raise ShardTooLarge(
                    f"第 {start} 页单页就有 "
                    f"{part.stat().st_size / 1024 / 1024:.1f}MB，"
                    f"超过 Graph 的 {settings.graph_max_shard_bytes / 1024 / 1024:.0f}MB "
                    f"上限且无法再切。请改用 LibreOffice 引擎重新上传。"
                )
            mid = start + pages // 2 - 1
            finer.extend([(start, mid), (mid + 1, end)])

        shutil.rmtree(out_dir, ignore_errors=True)
        ranges = finer
        parts = split_pptx(src, ranges, out_dir)

        still = [
            (ranges[i], p) for i, p in enumerate(parts)
            if p.stat().st_size > settings.graph_max_shard_bytes
        ]
        if still:
            (start, end), part = still[0]
            raise ShardTooLarge(
                f"页 {start}-{end} 切分后仍有 "
                f"{part.stat().st_size / 1024 / 1024:.1f}MB，"
                f"超过 Graph 的 "
                f"{settings.graph_max_shard_bytes / 1024 / 1024:.0f}MB 上限。"
                f"请改用 LibreOffice 引擎重新上传。"
            )

    task.shard_total = len(ranges)
    shard_ids = []
    for index, (start, end) in enumerate(ranges):
        sid = str(uuid.uuid4())
        session.add(
            TaskShard(
                shard_id=sid, task_id=task.task_id, index=index,
                page_start=start, page_end=end, status="pending",
            )
        )
        shard_ids.append(sid)
    _set_status(session, task, "converting")

    enqueue_shards(task.task_id, shard_ids)
    logger.info(
        "task sharded id=%s 分成 %d 片", task.task_id, len(shard_ids)
    )
```

模块顶部补 import：

```python
import shutil
import uuid
from pathlib import Path

from app.errors import ShardTooLarge
from app.models import Task, TaskShard
from app.queue import enqueue_shards
from app.services.pptx_split import split_pptx
from app.services.shard_planner import needs_sharding, plan_ranges
from app.services.shard_pipeline import shard_dir
```

**两处时机要确认清楚**：

`run_task` 的 `finally` 在切片路径 `return` 之后仍会执行，里面有三件事：

- `drop_original(task_id)` — **可以删**。切片已在 `_dispatch_shards` 里完成、分片 pptx 已落盘到 `storage/shards/`，与 `originals/` 是不同目录，后续的 `convert_shard` 读的是分片文件，不再需要原文件。
- `purge_expired_outputs()` — 无影响。
- `purge_expired_shards()` — **不会误删刚建的分片目录**：它按 mtime 判过期（`OUTPUT_TTL_HOURS`，默认 24 小时），刚创建的目录 mtime 是当前时间。

`_dispatch_shards` 抛异常时（`ShardTooLarge` 或切片本身失败），异常会被 `run_task` 的 `except AppError` 捕获并落 `failed`，而上面那个 `try/except` 负责把已写出的分片文件清掉。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline_sharding.py -q`
Expected: 5 passed

- [ ] **Step 6: 修既有测试的引擎期望**

`select_engine` 现在会给小文件返回 `"graph"`，一期二期有测试隐含期望 `"libreoffice"`。`conftest.py` 里的 `_force_placeholder_engine` fixture 已经把它固定成 `"placeholder"`，所以既有测试不受影响。跑全量确认：

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 93 passed

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/pipeline.py backend/app/services/engine_router.py backend/tests/test_pipeline_sharding.py
git commit -m "feat(backend): 主流水线接入切片分支与引擎自动路由"
```

---

### Task 9: API DTO 与保留策略扩展

**Files:**
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/api/tasks.py`
- Modify: `backend/app/services/retention.py`
- Modify: `backend/openapi.json`（脚本重新生成）
- Test: `backend/tests/test_retention_shards.py`

**Interfaces:**
- Consumes: `TaskShard`、`settings.shards_dir`
- Produces: `TaskDto.shard_total: int | None`、`TaskDto.shard_done: int`、`purge_expired_shards() -> int`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_retention_shards.py`：

```python
import os
import time

import pytest

from app.config import settings
from app.services.retention import purge_expired_shards


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    settings.ensure_dirs()
    return tmp_path


def test_purges_expired_shard_dirs(storage, monkeypatch):
    monkeypatch.setattr(settings, "output_ttl_hours", 24)
    old = settings.shards_dir / "old-task"
    old.mkdir(parents=True)
    (old / "000.pptx").write_bytes(b"x")
    past = time.time() - 25 * 3600
    os.utime(old, (past, past))

    fresh = settings.shards_dir / "fresh-task"
    fresh.mkdir(parents=True)
    (fresh / "000.pptx").write_bytes(b"x")

    assert purge_expired_shards() == 1
    assert not old.exists()
    assert fresh.exists()


def test_missing_dir_is_not_an_error(storage):
    import shutil

    shutil.rmtree(settings.shards_dir)
    assert purge_expired_shards() == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_retention_shards.py -q`
Expected: FAIL — `ImportError: cannot import name 'purge_expired_shards'`

- [ ] **Step 3: 扩展保留策略**

`backend/app/services/retention.py` 追加：

```python
def purge_expired_shards() -> int:
    """清理过期的分片目录，返回删除数量。

    正常路径下 merge_shards 的 finally 会删掉自己的分片目录。这个函数
    收拾的是异常路径的残骸：worker 被 OOM killer 干掉时，分片目录会留下
    几十 MB 到几百 MB（分片 pptx 加分片 PDF 是原文件的两倍体积），
    没有任何其他路径会碰它。
    """
    cutoff = time.time() - settings.output_ttl_hours * 3600
    removed = 0
    try:
        candidates = list(settings.shards_dir.iterdir())
    except OSError:
        return 0

    for path in candidates:
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError as exc:
            logger.warning("删除过期分片目录失败 %s: %s", path, exc)
    return removed
```

顶部补 `import shutil`。

`backend/app/services/pipeline.py` 的 `run_task` 的 `finally` 里，在 `purge_expired_outputs()` 之后加一行 `purge_expired_shards()`，并更新 import。

- [ ] **Step 4: 扩展 DTO**

`backend/app/schemas.py` 的 `TaskDto` 在 `options` 之后追加：

```python
    shard_total: int | None = None
    """切片总数。None 表示未切片。"""
    shard_done: int = 0
    """已完成的分片数，由查询时现算。"""
```

`backend/app/api/tasks.py` 的 `get_task` 构造 `TaskDto` 时补两个字段：

```python
        shard_total=task.shard_total,
        shard_done=(
            session.query(TaskShard)
            .filter(TaskShard.task_id == task.task_id, TaskShard.status == "done")
            .count()
            if task.shard_total
            else 0
        ),
```

并补 import `from app.models import Task, TaskShard`。

- [ ] **Step 5: 重新生成契约快照**

Run（工作目录 `backend/`）：
```bash
.venv/Scripts/python.exe -m scripts.dump_openapi
```
Expected: `openapi.json` 的 `TaskDto` schema 里出现 `shard_total` 与 `shard_done`

- [ ] **Step 6: 跑全量回归并提交**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 95 passed

```bash
git add backend/app/schemas.py backend/app/api/tasks.py backend/app/services/retention.py backend/app/services/pipeline.py backend/openapi.json backend/tests/test_retention_shards.py
git commit -m "feat(backend): 分片进度进 DTO，保留策略覆盖分片目录"
```

---

### Task 10: 前端分片状态与长耗时提示

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/styles/tokens.css`
- Modify: `frontend/src/components/TaskCard.tsx`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/src/components/UploadDropzone.test.tsx`（不改，仅确认仍通过）

**Interfaces:**
- Consumes: `TaskDto.shard_total`、`shard_done`
- Produces: 紫色区分的长耗时任务卡片、上传前的启发式提示

- [ ] **Step 1: 加语义色**

`frontend/src/styles/tokens.css` 的浅色 `:root` 追加：

```css
  --c-notable: #7c3aed;
  --c-notable-soft: #f3ecfe;
```

深色的两处（`@media (prefers-color-scheme: dark)` 与 `:root[data-theme='dark']`）各追加：

```css
    --c-notable: #a78bfa;
    --c-notable-soft: #2a1f3d;
```

用途固定为「这个任务会比你预期的久」，不要挪作他用。

- [ ] **Step 2: 扩展 TaskDto 类型与状态**

`frontend/src/lib/api.ts` 的 `TaskDto`：

```ts
  status:
    | 'pending'
    | 'parsing'
    | 'queued'
    | 'splitting'
    | 'converting'
    | 'merging'
    | 'done'
    | 'failed'
```

并在 `options` 之后追加：

```ts
  shard_total: number | null
  shard_done: number
```

- [ ] **Step 3: 任务卡片区分长耗时任务**

`frontend/src/components/TaskCard.tsx`：

`STATUS` 表补两项：

```ts
  splitting: { label: '拆分中', badge: 'badge-accent' },
  merging: { label: '合并中', badge: 'badge-accent' },
```

组件内 `const status = STATUS[task.status]` 之后加：

```tsx
  const sharded = task.shard_total !== null && task.shard_total > 0
  const shardPct = sharded
    ? Math.round((task.shard_done / (task.shard_total as number)) * 100)
    : 0
```

外层容器加紫色左边框：

```tsx
    <div
      className="card"
      style={{
        padding: 'var(--space-4)',
        ...(sharded
          ? { borderLeft: '4px solid var(--c-notable)' }
          : null),
      }}
    >
```

把 `inFlight` 的不定长进度条替换为：

```tsx
      {inFlight && (
        <>
          {sharded && (
            <p
              style={{
                fontSize: 13,
                color: 'var(--c-notable)',
                marginTop: 'var(--space-3)',
              }}
            >
              已完成 {task.shard_done} / {task.shard_total} 片
            </p>
          )}
          <div
            className="sunken"
            style={{
              height: 4,
              marginTop: 'var(--space-2)',
              overflow: 'hidden',
            }}
          >
            <div
              style={
                sharded
                  ? {
                      width: `${shardPct}%`,
                      height: '100%',
                      background: 'var(--c-notable)',
                      borderRadius: 999,
                      transition: 'width 300ms ease',
                    }
                  : {
                      width: '35%',
                      height: '100%',
                      background: 'var(--c-accent)',
                      borderRadius: 999,
                      animation: 'indeterminate 1.4s ease-in-out infinite',
                    }
              }
            />
          </div>
        </>
      )}
```

- [ ] **Step 4: 上传前的启发式提示**

`frontend/src/App.tsx` 增加状态与提示。在 `const [options, setOptions] = ...` 之后加：

```tsx
  const [pendingFile, setPendingFile] = useState<File | null>(null)
```

`UploadDropzone` 的 `onFileSelected` 改为先记录再上传：

```tsx
  const handleFile = async (file: File) => {
    setError(null)
    setPendingFile(file)
    try {
      const { taskId } = await uploadFile(file, {
        engine,
        options,
        onProgress: setProgress,
        onPhase: setPhase,
      })
      setTaskIds((prev) => [taskId, ...prev])
      setProgress(null)
    } catch (err) {
      setProgress(null)
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setPendingFile(null)
    }
  }
```

在 `<UploadDropzone />` 之后插入提示（`GRAPH_SHARD_HINT_BYTES = 40 * 1024 * 1024`，在文件顶部定义）：

```tsx
        {engine === 'graph' &&
          pendingFile !== null &&
          pendingFile.size > GRAPH_SHARD_HINT_BYTES && (
            <div
              className="card"
              style={{
                padding: 'var(--space-3)',
                borderLeft: '4px solid var(--c-notable)',
                fontSize: 13,
                lineHeight: 1.6,
              }}
            >
              此文件较大，Graph 通道会将其切分后分批转换，耗时可能达到十几分钟。
              改用 LibreOffice 通常在一分钟内完成。
            </div>
          )}
```

这层判断只看**文件大小**——上传前拿不到页数（那要 probe 之后才知道）。所以它是启发式的：可能提示了最终没切片，也可能没提示却切了（页数超 80 但体积小）。这个不准确可以接受，它的作用是在用户还能改主意时给出警告，不是精确预测。

**不做剩余时间估算**：Graph 每片耗时受服务端排队与文件复杂度影响，波动可达数倍，一个不断跳变的「剩余 3 分钟」比不给更糟。分片计数本身已经是可信的进度信号。

- [ ] **Step 5: 跑前端回归**

Run（工作目录 `frontend/`）：
```bash
npm test
npm run build
```
Expected: 19 passed，build 成功

- [ ] **Step 6: 提交**

```bash
git add frontend/src/lib/api.ts frontend/src/styles/tokens.css frontend/src/components/TaskCard.tsx frontend/src/App.tsx
git commit -m "feat(frontend): 分片进度与长耗时任务的视觉区分"
```

---

### Task 11: 部署配置与文档

**Files:**
- Modify: `docker-compose.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: 前十个任务的产物
- Produces: worker 内存限额调整、Graph 通道的部署说明

- [ ] **Step 1: 提高 worker 内存限额**

`docker-compose.yml` 的 worker 服务：

```yaml
    deploy:
      replicas: ${WORKER_REPLICAS:-2}
      resources:
        limits:
          # 三期从 3G 提到 8G：流式切片本身内存恒定，但合并阶段 pypdf 要把
          # 多份 PDF 读进内存，几十 MB × 若干片仍然吃内存。24GB 机器上
          # 2 worker × 8G = 16G，留 8G 给 api、redis 与系统。
          memory: 8G
```

`environment` 段追加 `PPTX2PDF_SECRET_KEY: ${PPTX2PDF_SECRET_KEY:-}`（api 与 worker 两处都要，凭证读取在两边都可能发生）。

- [ ] **Step 2: 更新 README**

「关键配置」表格追加两行：

```markdown
| `PPTX2PDF_SECRET_KEY` | 空 | Graph 凭证的 Fernet 主密钥。未配置则 Graph 引擎不可用 |
| `PPTX2PDF_GRAPH_MAX_PAGES_PER_SHARD` | 80 | 每片页数上限，对 Graph 的 100 页硬限留余量 |
```

「保真度边界」一节后新增：

```markdown
## Graph 通道（三期）

Microsoft Graph 用微软自己的渲染服务转换，保真度是天花板。但它有硬限制：
约 100 页、约 50MB、45 秒同步超时，且没有异步 API。超出限制的文件会被
切分成多片分别转换再合并。

**使用前需要配置 Azure**（四期的管理页面会提供入口）：

1. 注册 Azure AD 应用，拿到 tenant_id / client_id / client_secret
2. 授予应用权限并管理员同意
3. 建一个专用 SharePoint 站点作为中转库
4. 生成 Fernet 密钥并写入 `PPTX2PDF_SECRET_KEY`：
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

**密钥丢失等于凭证全废**——存进数据库的 client secret 再也解不开，只能去
Azure 重新生成。请把 `PPTX2PDF_SECRET_KEY` 与 `.env` 一起妥善备份。

**权限的已知不确定性**：清理中转文件用的 `permanentDelete` 文档标注需要
`Files.ReadWrite.All` 或 `Sites.ReadWrite.All`（租户级宽权限），与最小权限
的 `Sites.Selected` 可能冲突；社区也有报告称它有时仍把文件送进回收站。
代码里做了降级（403 时退回普通 DELETE 并告警），四期实测后再收敛。
中转文件若进了回收站，记得定期清空——两级回收站仍占 SharePoint 配额。
```

「已知限制」一节追加：

```markdown
- Graph 通道的自动路由只覆盖小文件（≤80 页且 ≤40MB）；更大的文件要显式
  选择 Graph 引擎才会走切片路径，因为切片意味着数十次 HTTP 往返和几分钟等待
- 分片转换的中间产物体积是原文件的两倍（分片 pptx + 分片 PDF），汇总后立即
  清理；worker 异常退出时残留的分片目录由保留策略按 `OUTPUT_TTL_HOURS` 回收
```

「分期」表格把三期状态改为「进行中」。

- [ ] **Step 3: 跑完整回归**

Run:
```bash
cd backend && .venv/Scripts/python.exe -m pytest -q
cd ../frontend && npm test && npm run build
cd .. && docker compose config -q
```
Expected: 后端 95 passed，前端 19 passed，build 成功，compose 校验通过

- [ ] **Step 4: 提交**

```bash
git add docker-compose.yml README.md
git commit -m "docs: 三期部署配置与 Graph 通道说明"
```

---

## 三期完成判据

本机可验证（三期**不部署**）：

- [ ] 后端 `pytest -q` 95 passed
- [ ] 前端 `npm test` 19 passed，`npm run build` 成功
- [ ] `docker compose config -q` 通过
- [ ] `backend/openapi.json` 已更新并提交
- [ ] 切片测试证明 media 确实被裁掉（不是只删了 sldIdLst 条目）
- [ ] 合并测试证明页序由入参决定、与文件名排序无关

留四期真机验证（需要 Azure 凭证）：

- [ ] Graph 认证、上传、`?format=pdf` 转换、下载全链路
- [ ] `permanentDelete` 的实际权限表现，以及它是否真的绕过回收站
- [ ] 429 退避在真实限流下的行为
- [ ] 真实课件的 Graph 与 LibreOffice 保真度对比
