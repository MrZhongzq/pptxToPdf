# 五期实施计划：内嵌媒体剥离与手动触发转换

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 转换前剥掉内嵌视频/音频（PDF 本就放不了），并把「上传完自动转换」改成「传完先等着，选好引擎再点开始」。

**Architecture:** 先把 `pptx_split.py` 里六个通用的 OPC 工具函数提取到共享模块，媒体剥离复用它们（不重踩三期五轮修复的教训）；剥离后的文件覆盖原件，此后所有判断都基于它。上传侧新增 `ready` 状态与 `POST /api/tasks/{id}/start`，`complete` 不再入队。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / zipfile+ElementTree（无新依赖）、React 19 / vitest。

## Global Constraints

以下逐字来自 spec，每个任务的要求都隐含包含本节。

- 配置访问一律 `from app.config import settings` 单例。**本仓库没有 `get_settings()`**。
- 丢弃的关系类型**三个都要**（PowerPoint 嵌一段视频通常同时写 `video` 与 `media` 两条关系指向同一个 part，漏一个文件就还在包里）：
  ```
  http://schemas.openxmlformats.org/officeDocument/2006/relationships/video
  http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio
  http://schemas.microsoft.com/office/2007/relationships/media
  ```
- 剥离后的文件**覆盖原件**，此后 `size_bytes`、`needs_sharding`、切片、转换全部基于剥离后的文件。
- 不含媒体时**不重写文件**（`stripped=False`），避免零收益的解压重打包。
- 接受 slide 正文里指向已删媒体的 `r:id` **悬空**——同三期对「内部跳转悬空 rId」的既有裁决。
- `ready` **不进** `retention.NON_TERMINAL`，由新增的 `PPTX2PDF_READY_TTL_HOURS`（默认 **1**）回收。
- **不能复用 `TASK_NOT_READY`**：它已被 `download` 端点用于「还没转完、无可下载结果」，语义相反。新增 `TASK_ALREADY_STARTED`（409）。
- 新错误类**沿用既有位置**放 `app/api/tasks.py`（`TaskNotFound`/`TaskNotReady` 都在那儿），不顺手搬去 `errors.py`。
- 回收 ready 时标 `failed` + 新增的 `READY_EXPIRED`，**不复用** `TASK_ABANDONED`（那是「转换中卡死」，这是「你没点开始」）。
- 不做：上传卡住的三个缺陷、`p:timing` 动画分页、废弃 `POST /api/uploads` 的 `engine`/`options` 字段（用户明确要求保留）。
- 基线：后端 **293** passed，前端 **71** passed。任何既有测试不许变红。

## 验收命令（五条都要跑）

四期教训：`npm test` 与 `npm run build` 都绿而 `npm run lint` 有 9 条 error，而验收只跑了前两个。

```
cd backend && .venv/Scripts/python.exe -m pytest -q
cd frontend && npm test -- --run
cd frontend && npm run build
cd frontend && npm run lint
docker compose config -q
```

## 既有代码的真实签名（已逐一 grep 核对，勿凭记忆）

```python
# app/services/pptx_split.py —— 全部是下划线私有，Task 1 要提取前六个
PRESENTATION = "ppt/presentation.xml"
SLIDE_REL_TYPE = "...relationships/slide"
DROP_REL_TYPES = {notesSlide, comments, thumbnail}   # 三个
COPY_CHUNK = 1024 * 1024
def _rels_path(part: str) -> str
def _owner_part(rels_name: str) -> str
def _resolve(base_part: str, target: str) -> str
def _read_rels(zf: zipfile.ZipFile, part: str) -> list[tuple[str, str, str]]
def _rewrite_rels(raw: bytes, keep_parts: set[str], base_part: str) -> bytes
def _rewrite_content_types(raw: bytes, keep_parts: set[str]) -> bytes
def _collect(zf, part, keep) -> None          # 切片专有，不提取
def _slide_order(zf) -> list[tuple[str, str]] # 切片专有，不提取
def _rewrite_presentation(raw, keep_rids) -> bytes  # 切片专有，不提取
def split_pptx(src: Path, ranges: list[tuple[int,int]], out_dir: Path) -> list[Path]

# app/queue.py（注意：不是 app/services/queue.py）
def enqueue_conversion(task_id: str) -> None

# app/services/retention.py
NON_TERMINAL = ("pending", "parsing", "queued", "converting", "merging")
def drop_original(task_id: str) -> None
def purge_expired_outputs() -> int
def purge_expired_shards() -> int
def reap_stale_tasks() -> int

# app/api/tasks.py（错误类定义在这里，不在 errors.py）
class TaskNotFound(AppError):  code="TASK_NOT_FOUND";  http_status=404
class TaskNotReady(AppError):  code="TASK_NOT_READY";  http_status=409  # 已被 download 占用
def _load(session, task_id) -> Task

# app/schemas.py
class CompleteResponse(BaseModel):
    task_id: str
class ConversionOptions(BaseModel):
    expand_animations: bool = False   # 一期就定的契约，后端只落库不实现
```

前端：`useTaskPolling.ts:8` 是 `const TERMINAL = new Set<TaskDto['status']>(['done','failed'])`；`TaskCard.test.tsx:38` 的 `ALL_STATUSES` 现列 7 个值。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `backend/app/services/opc_rewrite.py`（新） | 六个通用 OPC 工具函数，`pptx_split` 与 `media_strip` 共用 |
| `backend/app/services/pptx_split.py`（改） | 删掉被提取的六个，改为 import；切片专有逻辑原样保留 |
| `backend/app/services/media_strip.py`（新） | `strip_media(src) -> StripResult`，就地重写 |
| `backend/app/services/pipeline.py`（改） | `probe` 之前调 `strip_media` |
| `backend/app/api/uploads.py`（改） | `complete` 不再入队，Task 落 `ready` |
| `backend/app/api/tasks.py`（改） | 新增 `POST /{id}/start` 与 `TaskAlreadyStarted` |
| `backend/app/services/retention.py`（改） | 新增 `purge_expired_ready()` |
| `backend/app/config.py`（改） | 新增 `ready_ttl_hours` |
| `backend/app/errors.py`（改） | 新增 `ReadyExpired` |
| `frontend/src/lib/api.ts`（改） | `status` 联合类型加 `ready`；新增 `startTask()` |
| `frontend/src/components/ReadyCard.tsx`（新） | 已就绪卡片：引擎选择 + 选项 + 开始转换 |
| `frontend/src/App.tsx`（改） | 传完停在 ReadyCard，不自动转 |

---

## Task 1: 提取通用 OPC 工具

**Files:**
- Create: `backend/app/services/opc_rewrite.py`
- Modify: `backend/app/services/pptx_split.py`
- Test: 复用既有 `backend/tests/test_pptx_split.py`（14 个，三期五轮修复的成果，是最好的回归网）

**Interfaces:**
- Produces: `rels_path`、`owner_part`、`resolve`、`read_rels`、`rewrite_rels`、`rewrite_content_types`（去掉下划线，成为公开 API）；常量 `REL_NS_URI`、`CT_NS_URI`、`CT_NS`

**为什么要提取**：媒体剥离要复用这些函数。它们是纯 OPC 工具（「包里关系怎么解析」），不含任何切片语义（「按页范围裁剪」），先被切片用上只是历史顺序。跨模块 import 私有函数是坏味道；重写一套则要重踩三期那五轮教训（悬空 rels、`mc:Ignorable` 丢失、正则手术的三种变体）。

**不提取的**：`_collect`、`_slide_order`、`_rewrite_presentation`、`_SLD_ID_RE` 是切片专有，留在 `pptx_split.py`。

- [ ] **Step 1: 先跑一次既有测试，记下基线**

```
cd backend && .venv/Scripts/python.exe -m pytest tests/test_pptx_split.py -q
```
预期：`14 passed`。这 14 个是本任务唯一的验收标准——提取是纯搬运，行为一个字都不能变。

- [ ] **Step 2: 建 `opc_rewrite.py`，把六个函数搬过去**

创建 `backend/app/services/opc_rewrite.py`。从 `pptx_split.py` 剪切以下内容，**函数体一行不改**，只去掉名字前的下划线：

- 四个常量（`pptx_split.py:16-19`，原样搬）：
  ```python
  REL_NS_URI = "http://schemas.openxmlformats.org/package/2006/relationships"
  CT_NS_URI = "http://schemas.openxmlformats.org/package/2006/content-types"
  REL_NS = "{" + REL_NS_URI + "}"
  CT_NS = "{" + CT_NS_URI + "}"
  ```
  `REL_NS` 若 `pptx_split` 的切片专有逻辑还在用，两边都能 import 到，不必留副本。
- `_rels_path` → `rels_path`
- `_owner_part` → `owner_part`
- `_resolve` → `resolve`
- `_read_rels` → `read_rels`
- `_rewrite_rels` → `rewrite_rels`
- `_rewrite_content_types` → `rewrite_content_types`

模块 docstring：

```python
"""OPC（Open Packaging Conventions）包重写的通用工具。

这些函数处理的是「包里的关系怎么解析、怎么删」，与「按页范围裁剪」
无关——它们先住在 pptx_split.py 里只是历史顺序。媒体剥离需要同一套
机制，故提取共用：跨模块 import 私有函数是坏味道，而各写一套会让
第二份重新踩一遍三期在切片上修了五轮的那些坑（悬空 Relationship、
mc:Ignorable 被 ET 往返吃掉、rels 里 Target 相对路径的解析基准）。
"""
```

注意 `read_rels` 内部调用 `_rels_path`、`_resolve`，搬过去后要改成调用去掉下划线的版本。

- [ ] **Step 3: `pptx_split.py` 改为 import**

删掉那六个函数定义与四个常量，在顶部加：

```python
from app.services.opc_rewrite import (
    CT_NS,
    REL_NS,
    owner_part,
    read_rels,
    rels_path,
    resolve,
    rewrite_content_types,
    rewrite_rels,
)
```

**六个全都要 import**——已核对调用点：`_resolve` 在第 76、220 行有调用，`_owner_part` 在第 316 行有调用，不是只在被提取的函数内部用。

然后把所有调用点改名（`_rels_path(` → `rels_path(`、`_resolve(` → `resolve(`、`_owner_part(` → `owner_part(` 等）。

**用 grep 确认没有漏网的调用点**：

```
grep -n "_rels_path\|_owner_part\|_resolve\|_read_rels\|_rewrite_rels\|_rewrite_content_types" app/services/pptx_split.py
```
预期：无输出。

- [ ] **Step 4: 跑测试确认零行为变化**

```
.venv/Scripts/python.exe -m pytest tests/test_pptx_split.py -q
```
预期：`14 passed`，与 Step 1 完全一致。

任何一条变红都说明提取不是纯搬运，回去检查是不是改动了函数体。

- [ ] **Step 5: 跑全量**

```
.venv/Scripts/python.exe -m pytest -q
```
预期：`293 passed`

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/opc_rewrite.py backend/app/services/pptx_split.py
git commit -m "refactor(backend): 提取通用 OPC 重写工具供媒体剥离复用"
```

---

## Task 2: 媒体剥离

**Files:**
- Create: `backend/app/services/media_strip.py`
- Test: `backend/tests/test_media_strip.py`

**Interfaces:**
- Consumes: `opc_rewrite` 的六个函数；`pptx_split.COPY_CHUNK`
- Produces:
  ```python
  @dataclass(frozen=True)
  class StripResult:
      stripped: bool
      removed_parts: int
      bytes_before: int
      bytes_after: int

  MEDIA_REL_TYPES: frozenset[str]
  def strip_media(src: Path) -> StripResult   # 就地重写
  ```

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_media_strip.py`：

```python
import zipfile
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches, Pt

from app.services.media_strip import MEDIA_REL_TYPES, strip_media
from app.services.opc_rewrite import read_rels, rels_path

VIDEO_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/video"
MEDIA_REL = "http://schemas.microsoft.com/office/2007/relationships/media"


def _plain_deck(path: Path, pages: int = 3) -> Path:
    """不含媒体的普通 deck。"""
    prs = Presentation()
    for i in range(pages):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
        box.text_frame.text = f"PAGE-{i + 1}"
        box.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
    prs.save(str(path))
    return path


def _deck_with_fake_video(path: Path, payload_mb: int = 2) -> Path:
    """造一个带假视频 part 的 deck。

    python-pptx 没有直接加视频的稳定 API，所以先造普通 deck，再用
    zipfile 手工塞入 media part、在 slide1 的 rels 里加两条关系
    （video + media，PowerPoint 真实产出就是两条都写），并往
    [Content_Types].xml 加一条 Default。
    """
    tmp = path.with_suffix(".base.pptx")
    _plain_deck(tmp, pages=3)

    payload = b"\x00" * (payload_mb * 1024 * 1024)
    with zipfile.ZipFile(tmp) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            raw = zin.read(item.filename)
            if item.filename == "ppt/slides/_rels/slide1.xml.rels":
                injected = (
                    f'<Relationship Id="rIdVid" Type="{VIDEO_REL}" Target="../media/movie1.mp4"/>'
                    f'<Relationship Id="rIdMed" Type="{MEDIA_REL}" Target="../media/movie1.mp4"/>'
                ).encode()
                raw = raw.replace(b"</Relationships>", injected + b"</Relationships>")
            elif item.filename == "[Content_Types].xml":
                raw = raw.replace(
                    b"</Types>",
                    b'<Default Extension="mp4" ContentType="video/mp4"/></Types>',
                )
            zout.writestr(item, raw)
        zout.writestr("ppt/media/movie1.mp4", payload)

    tmp.unlink()
    return path


def _parts(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


def test_media_rel_types_covers_video_audio_and_media():
    # PowerPoint 嵌一段视频会同时写 video 与 media 两条关系指向同一个 part，
    # 漏一个文件就还留在包里
    assert VIDEO_REL in MEDIA_REL_TYPES
    assert MEDIA_REL in MEDIA_REL_TYPES
    assert any("audio" in t for t in MEDIA_REL_TYPES)


def test_strips_video_part(tmp_path):
    deck = _deck_with_fake_video(tmp_path / "v.pptx", payload_mb=2)
    before = _parts(deck)
    assert "ppt/media/movie1.mp4" in before

    result = strip_media(deck)

    assert result.stripped is True
    assert result.removed_parts == 1
    assert result.bytes_after < result.bytes_before
    assert "ppt/media/movie1.mp4" not in _parts(deck)


def test_keeps_all_slides(tmp_path):
    deck = _deck_with_fake_video(tmp_path / "v.pptx")
    strip_media(deck)
    slides = [n for n in _parts(deck) if n.startswith("ppt/slides/slide")]
    assert len(slides) == 3


def test_no_dangling_internal_relationships(tmp_path):
    deck = _deck_with_fake_video(tmp_path / "v.pptx")
    strip_media(deck)
    with zipfile.ZipFile(deck) as zf:
        names = set(zf.namelist())
        for name in list(names):
            if not name.endswith(".rels"):
                continue
            owner = name.replace("/_rels/", "/").replace(".rels", "")
            for _rid, _type, target in read_rels(zf, owner):
                assert target in names, f"{name} 指向不存在的 {target}"


def test_result_is_openable(tmp_path):
    deck = _deck_with_fake_video(tmp_path / "v.pptx")
    strip_media(deck)
    prs = Presentation(str(deck))
    assert len(prs.slides) == 3


def test_deck_without_media_is_not_rewritten(tmp_path):
    deck = _plain_deck(tmp_path / "p.pptx")
    before_bytes = deck.read_bytes()

    result = strip_media(deck)

    assert result.stripped is False
    assert result.removed_parts == 0
    # 零收益时不该白做一次解压重打包
    assert deck.read_bytes() == before_bytes


def test_preserves_mc_ignorable(tmp_path):
    """ET 往返会丢弃未使用的 xmlns 声明，而 mc:Ignorable 指着它们。
    presentation.xml 必须逐字节保留——这是三期修了一整轮的坑。"""
    deck = _deck_with_fake_video(tmp_path / "v.pptx")
    with zipfile.ZipFile(deck) as zf:
        pres_before = zf.read("ppt/presentation.xml")

    strip_media(deck)

    with zipfile.ZipFile(deck) as zf:
        assert zf.read("ppt/presentation.xml") == pres_before
```

- [ ] **Step 2: 跑测试确认失败**

```
.venv/Scripts/python.exe -m pytest tests/test_media_strip.py -q
```
预期：`ModuleNotFoundError: No module named 'app.services.media_strip'`

- [ ] **Step 3: 实现**

创建 `backend/app/services/media_strip.py`：

```python
"""剥离 pptx 里的内嵌视频与音频。

PDF 放不了视频——这些字节从进入系统的第一刻起就是纯浪费，却会让
一个本来能转的 deck 撞上分片上限（真机遇到过：83.7MB 的课件里第 25
页单页 56MB 视频，报 SHARD_TOO_LARGE 且单页无法再切分）。

复用 opc_rewrite 的通用工具，不另写一套：三期在切片上为同一类问题
修了五轮（悬空 Relationship、mc:Ignorable 被 ET 往返吃掉、正则手术
对空格/命名空间前缀/非自闭合三种变体的处理），那些教训不该重走。
"""

import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.services.opc_rewrite import (
    owner_part,
    read_rels,
    rewrite_content_types,
    rewrite_rels,
)
from app.services.pptx_split import COPY_CHUNK

MEDIA_REL_TYPES = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/video",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio",
        # PowerPoint 嵌一段视频会同时写 video 与 media 两条关系指向同一个
        # part——只丢一条的话文件还留在包里，白忙一场。
        "http://schemas.microsoft.com/office/2007/relationships/media",
    }
)


@dataclass(frozen=True)
class StripResult:
    stripped: bool
    """是否真的删掉了东西。False 时文件未被重写。"""
    removed_parts: int
    bytes_before: int
    bytes_after: int


def _media_targets(zf: zipfile.ZipFile) -> set[str]:
    """扫描全部 .rels，收集被媒体关系指向的 part。"""
    targets: set[str] = set()
    for name in zf.namelist():
        if not name.endswith(".rels"):
            continue
        owner = owner_part(name)
        for _rid, rel_type, target in read_rels(zf, owner):
            if rel_type in MEDIA_REL_TYPES:
                targets.add(target)
    return targets


def strip_media(src: Path) -> StripResult:
    """就地剥离内嵌媒体。不含媒体时不重写文件。

    留在 slide 正文里指向已删媒体的 r:id 会悬空——同三期对「内部跳转
    悬空 rId」的既有裁决：消费方忽略非关键内容，而不剥的代价是那份
    deck 根本转不了。
    """
    bytes_before = src.stat().st_size

    with zipfile.ZipFile(src) as zf:
        drop = _media_targets(zf)
        if not drop:
            return StripResult(False, 0, bytes_before, bytes_before)
        keep_parts = {n for n in zf.namelist() if n not in drop and not n.endswith("/")}

    tmp = Path(tempfile.mkstemp(suffix=".pptx", dir=str(src.parent))[1])
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
            tmp, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            for item in zin.infolist():
                name = item.filename
                if name in drop:
                    continue
                if name == "[Content_Types].xml":
                    zout.writestr(item, rewrite_content_types(zin.read(name), keep_parts))
                elif name.endswith(".rels"):
                    zout.writestr(
                        item, rewrite_rels(zin.read(name), keep_parts, owner_part(name))
                    )
                else:
                    # 其余 part 逐字节流式复制——presentation.xml 必须原样，
                    # ET 往返会丢掉 mc:Ignorable 指向的 xmlns 声明。
                    with zin.open(item) as fsrc, zout.open(item, "w") as fdst:
                        shutil.copyfileobj(fsrc, fdst, COPY_CHUNK)
        tmp.replace(src)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    return StripResult(True, len(drop), bytes_before, src.stat().st_size)
```

- [ ] **Step 4: 跑测试确认通过**

```
.venv/Scripts/python.exe -m pytest tests/test_media_strip.py -q
```
预期：`7 passed`

- [ ] **Step 5: 变异检查**

逐个做，每次改回，把实际输出写进报告：

1. `MEDIA_REL_TYPES` 去掉 `media`（只留 video/audio）→ 预期 `test_media_rel_types_covers_video_audio_and_media` 变红
2. `if not drop: return` 那段删掉（改成总是重写）→ 预期 `test_deck_without_media_is_not_rewritten` 变红
3. `.rels` 分支改成逐字节复制（不重写）→ 预期 `test_no_dangling_internal_relationships` 变红
4. `presentation.xml` 也走 `rewrite_rels`（模拟 ET 往返）→ 预期 `test_preserves_mc_ignorable` 变红

- [ ] **Step 6: 跑全量并提交**

```
.venv/Scripts/python.exe -m pytest -q
```
预期：`300 passed`（293 + 7）

```bash
git add backend/app/services/media_strip.py backend/tests/test_media_strip.py
git commit -m "feat(backend): 剥离内嵌视频与音频"
```

---

## Task 3: 接进流水线

**Files:**
- Modify: `backend/app/services/pipeline.py`
- Test: `backend/tests/test_pipeline_media_strip.py`

**Interfaces:**
- Consumes: `strip_media(src) -> StripResult`

**这个任务的核心是「剥离结果真的取代了原件」**，不只是「调了一下」。

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_pipeline_media_strip.py`。参照 `tests/test_pipeline_sharding.py` 里建 Task 与假引擎的既有写法（先读它，别自己发明新模式）。

```python
def test_run_task_strips_media_before_probe(tmp_path, monkeypatch, ...):
    """剥离必须发生在 probe 之前——size_bytes 与切片判定都要看剥离后的值。"""
    calls = []

    def fake_strip(src):
        calls.append(("strip", src.stat().st_size))
        # 模拟剥掉一半
        src.write_bytes(src.read_bytes()[: src.stat().st_size // 2])
        return StripResult(True, 1, 100, 50)

    def fake_probe(src):
        calls.append(("probe", src.stat().st_size))
        return PptxMeta(slide_count=3, slide_width_emu=1, slide_height_emu=1, fonts=set())

    monkeypatch.setattr(pipeline, "strip_media", fake_strip)
    monkeypatch.setattr(pipeline, "probe", fake_probe)
    run_task(task_id)

    assert [c[0] for c in calls] == ["strip", "probe"], "剥离必须在 probe 之前"
    assert calls[1][1] < calls[0][1], "probe 看到的应是剥离后的文件"


def test_size_bytes_reflects_stripped_file(...):
    """task.size_bytes 记的是剥离后的体积，不是用户上传的原始体积。"""


def test_sharding_decision_uses_stripped_size(...):
    """一个原始 >40MB、剥离后 <40MB 的 deck 不该走切片路径。
    这正是真机那份 83.7MB 课件要走的路。"""
```

三条测试的完整实现由实现者按仓库既有 fixture 写法补全，但断言的语义必须是上面这三条。

- [ ] **Step 2: 跑测试确认失败**

预期：`AttributeError: module 'app.services.pipeline' has no attribute 'strip_media'`

- [ ] **Step 3: 接进 `run_task`**

在 `pipeline.py` 顶部加 import：

```python
from app.services.media_strip import strip_media
```

在 `_set_status(session, task, "parsing")` 之后、`meta = probe(src)` **之前**插入：

```python
            # 剥离内嵌媒体。必须在 probe 之前：PDF 放不了视频，而那些
            # 字节会让 size_bytes 虚高、把本来单次能转的 deck 推进切片
            # 路径甚至撞上分片上限。剥离后的文件覆盖原件，此后所有判断
            # （size_bytes / needs_sharding / 切片 / 转换）都基于它。
            strip = strip_media(src)
            if strip.stripped:
                logger.info(
                    "media stripped id=%s parts=%d %d -> %d bytes",
                    task_id, strip.removed_parts, strip.bytes_before, strip.bytes_after,
                )
```

注意 `size_bytes = src.stat().st_size` 那行本来就在 `probe` 之后，剥离后它自然读到新体积——**确认这一点，不要重复计算**。

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 变异检查（本任务验收核心）**

1. 删掉 `strip = strip_media(src)` 整行 → 预期 `test_run_task_strips_media_before_probe` 变红
2. 把 `strip_media` 调用挪到 `probe` **之后** → 预期同一条变红（顺序断言）
3. 把 `strip_media` 换成空操作（返回 `StripResult(False, 0, n, n)` 且不改文件）→ 预期 `test_sharding_decision_uses_stripped_size` 变红

- [ ] **Step 6: 跑全量并提交**

```bash
git add backend/app/services/pipeline.py backend/tests/test_pipeline_media_strip.py
git commit -m "feat(backend): 转换前剥离内嵌媒体，剥离结果取代原件"
```

---

## Task 4: ready 状态与 start 端点

**Files:**
- Modify: `backend/app/api/uploads.py`、`backend/app/api/tasks.py`、`backend/app/schemas.py`
- Test: `backend/tests/test_task_start.py`

**Interfaces:**
- Produces: `POST /api/tasks/{task_id}/start`；`TaskAlreadyStarted`（`TASK_ALREADY_STARTED`，409）；`StartTaskRequest`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_task_start.py`：

```python
def test_complete_leaves_task_ready_and_does_not_enqueue(client, monkeypatch):
    """complete 只拼装落库，不入队。"""
    enqueued = []
    monkeypatch.setattr("app.api.uploads.enqueue_conversion", lambda t: enqueued.append(t))
    task_id = _upload_a_deck(client)
    task = _get_task(task_id)
    assert task["status"] == "ready"
    assert enqueued == [], "complete 不该入队"


def test_start_enqueues_and_moves_to_pending(client, monkeypatch):
    enqueued = []
    monkeypatch.setattr("app.api.tasks.enqueue_conversion", lambda t: enqueued.append(t))
    task_id = _upload_a_deck(client)
    resp = client.post(f"/api/tasks/{task_id}/start", json={"engine": "libreoffice"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert enqueued == [task_id]


def test_start_records_engine_and_options(client, monkeypatch):
    """引擎与选项在 start 时才定——这正是本期的目的。"""
    monkeypatch.setattr("app.api.tasks.enqueue_conversion", lambda t: None)
    task_id = _upload_a_deck(client)
    client.post(
        f"/api/tasks/{task_id}/start",
        json={"engine": "graph", "options": {"expand_animations": True}},
    )
    task = _load_task_row(task_id)
    assert task.requested_engine == "graph"
    assert "expand_animations" in (task.options_json or "")


def test_start_twice_is_409(client, monkeypatch):
    enqueued = []
    monkeypatch.setattr("app.api.tasks.enqueue_conversion", lambda t: enqueued.append(t))
    task_id = _upload_a_deck(client)
    client.post(f"/api/tasks/{task_id}/start", json={})
    resp = client.post(f"/api/tasks/{task_id}/start", json={})
    assert resp.status_code == 409
    assert resp.json()["code"] == "TASK_ALREADY_STARTED"
    assert len(enqueued) == 1, "不该重复入队"


def test_start_on_missing_task_is_404(client):
    resp = client.post("/api/tasks/does-not-exist/start", json={})
    assert resp.status_code == 404
    assert resp.json()["code"] == "TASK_NOT_FOUND"


def test_start_drops_original_when_enqueue_fails(client, monkeypatch):
    """Redis 挂了时必须删原文件——这段兜底是从 complete 挪过来的，
    漏挪的话每次 Redis 抖动都留一份 80-500MB 的孤儿。"""
    dropped = []
    monkeypatch.setattr("app.api.tasks.drop_original", lambda t: dropped.append(t))
    def boom(_):
        raise ConnectionError("redis down")
    monkeypatch.setattr("app.api.tasks.enqueue_conversion", boom)

    task_id = _upload_a_deck(client)
    resp = client.post(f"/api/tasks/{task_id}/start", json={})
    assert resp.status_code == 503
    assert dropped == [task_id]
    assert _load_task_row(task_id).status == "failed"
```

`_upload_a_deck` / `_get_task` / `_load_task_row` 三个 helper 按 `tests/test_tasks_api.py` 的既有写法实现。

- [ ] **Step 2: 跑测试确认失败**

预期：`complete` 后 status 是 `pending` 而非 `ready`，且 `/start` 是 404。

- [ ] **Step 3: 改 `complete_upload`**

在 `uploads.py` 里：

- Task 的 `status=` 从 `"pending"` 改成 `"ready"`
- **删掉** `enqueue_conversion(task_id)` 那整段 `try/except`（含 Redis 兜底）——它整体挪到 `start`
- `store().purge(upload_id)` 保留

改完后 `complete_upload` 的尾部是：

```python
    session.add(task)
    session.commit()
    store().purge(upload_id)
    return CompleteResponse(task_id=task_id)
```

- [ ] **Step 4: 加 `start` 端点**

在 `schemas.py` 加：

```python
class StartTaskRequest(BaseModel):
    """引擎与选项在 start 时才提交——上传时不必先想好。"""

    engine: str | None = Field(default=None, min_length=1, max_length=32)
    options: ConversionOptions | None = None
```

在 `tasks.py` 加错误类（**放这里，不放 errors.py**——`TaskNotFound`/`TaskNotReady` 都在这个文件，沿用既有位置）：

```python
class TaskAlreadyStarted(AppError):
    """任务已经离开 ready 状态。

    不复用 TASK_NOT_READY——那个码已被 download 用于「还没转完、无可
    下载结果」，语义相反：一个是还没到终点，一个是已经离开起点。
    """

    code = "TASK_ALREADY_STARTED"
    http_status = 409
```

端点：

```python
@router.post("/{task_id}/start", response_model=TaskDto, responses=TASK_ERRORS)
def start_task(
    task_id: str,
    payload: StartTaskRequest,
    session: Session = Depends(get_session),
) -> TaskDto:
    task = _load(session, task_id)
    if task.status != "ready":
        raise TaskAlreadyStarted(f"任务状态为 {task.status}，无法重复启动")

    task.requested_engine = payload.engine
    if payload.options is not None:
        task.options_json = payload.options.model_dump_json()
    task.status = "pending"
    session.commit()

    try:
        enqueue_conversion(task_id)
    except Exception as exc:
        # 这段兜底从 complete_upload 挪过来。任务不会入队也就永远走不到
        # run_task 的 finally——那是原文件唯一的删除路径，不显式删就留下
        # 一份 80-500MB 的孤儿。
        task.status = "failed"
        task.error_code = EngineUnavailable.code
        task.error_message = f"任务排队失败，转换服务暂不可用: {exc}"
        session.commit()
        drop_original(task_id)
        raise EngineUnavailable(f"任务排队失败，转换服务暂不可用: {exc}") from exc

    session.refresh(task)
    return _to_dto(task)
```

`TASK_ERRORS` 字典要加一条 409 的 `TASK_ALREADY_STARTED` 描述（它现在只有 404/409/410 三条，其中 409 写的是 `TASK_NOT_READY`——改成同时列出两个码）。

`_to_dto` 若不存在，按 `tasks.py` 里 `get_task` 现有的 DTO 组装方式写。

- [ ] **Step 5: 跑测试确认通过**

- [ ] **Step 6: 变异检查（本任务验收核心）**

1. `complete_upload` 里加回 `enqueue_conversion` → 预期 `test_complete_leaves_task_ready_and_does_not_enqueue` 变红
2. `start` 里的 `if task.status != "ready"` 删掉 → 预期 `test_start_twice_is_409` 变红
3. `start` 里的 `drop_original(task_id)` 删掉 → 预期 `test_start_drops_original_when_enqueue_fails` 变红
4. `task.requested_engine = payload.engine` 删掉 → 预期 `test_start_records_engine_and_options` 变红

- [ ] **Step 7: 跑全量并提交**

```bash
git add backend/app/api/uploads.py backend/app/api/tasks.py backend/app/schemas.py backend/tests/test_task_start.py
git commit -m "feat(backend): ready 状态与 start 端点，上传完不再自动转换"
```

---

## Task 5: ready 的 TTL 回收

**Files:**
- Modify: `backend/app/config.py`、`backend/app/errors.py`、`backend/app/services/retention.py`、`backend/app/main.py`、`backend/app/services/pipeline.py`、`.env.example`
- Test: `backend/tests/test_retention_ready.py`

**Interfaces:**
- Produces: `settings.ready_ttl_hours`（默认 1）；`ReadyExpired`（`READY_EXPIRED`，410）；`purge_expired_ready() -> int`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_retention_ready.py`：

```python
def test_fresh_ready_task_is_kept(db_session, monkeypatch):
    """59 分钟的 ready 任务不该被回收。"""


def test_expired_ready_task_is_reaped(db_session, monkeypatch, tmp_path):
    """61 分钟的 ready 任务：原文件删掉、Task 落 failed + READY_EXPIRED。"""
    # 断言：
    #   purge_expired_ready() == 1
    #   task.status == "failed"
    #   task.error_code == "READY_EXPIRED"
    #   原文件不存在


def test_ready_is_not_touched_by_stale_reaper(db_session, monkeypatch):
    """孤儿回收器（45 分钟）管的是「转换卡住」，ready 只是在等人点按钮。
    造一个 46 分钟没动的 ready 任务，reap_stale_tasks 应返回 0。"""


def test_ready_ttl_does_not_touch_unfinished_uploads(db_session, tmp_path):
    """ready TTL 不许误伤未完成的上传会话——那归 upload TTL（24 小时）管，
    调短它会让大文件传到一半、暂停超时后必须从头重传。
    造一个 2 小时没动的 active upload，跑 purge_expired_ready 后断言它仍在。"""


def test_non_ready_tasks_are_not_touched(db_session):
    """converting / done / failed 的任务不受 ready TTL 影响。"""
```

五条测试的完整实现按 `tests/test_retention_shards.py` 的既有写法补全（先读它——那里有建过期任务、monkeypatch TTL 的现成模式）。

**注意 TTL 的 monkeypatch 值不要设成等于默认值**，否则那条 setattr 什么也没钉住——四期在这上面出过一次（把 `output_ttl_hours` monkeypatch 成 24，而默认就是 24，导致「读错配置项」的变异全绿通过）。

- [ ] **Step 2: 跑测试确认失败**

预期：`ImportError: cannot import name 'purge_expired_ready'`

- [ ] **Step 3: 加配置项**

`config.py` 里 `upload_ttl_hours` 附近加：

```python
    # ready 任务（传完但没点「开始转换」）的原文件保留时长。
    # 不复用 upload_ttl_hours：那个管的是未完成的上传会话、支持断点续传，
    # 调短会让大文件传到一半、暂停超时后必须从头重传。ready 的重传成本
    # 小得多，可以更快回收——机器盘不大，单份原件可能 80-500MB。
    ready_ttl_hours: int = 1
```

- [ ] **Step 4: 加错误类**

`errors.py` 末尾加：

```python
class ReadyExpired(AppError):
    """上传后超时未点「开始转换」，原文件已被回收。

    与 TASK_ABANDONED 是两个不同的失败原因，不要混用：那个是「转换
    过程中卡死了」，这个是「你没点开始」。
    """

    code = "READY_EXPIRED"
    http_status = 410
```

- [ ] **Step 5: 实现回收**

`retention.py` 加（参照同文件 `purge_expired_shards` 的写法）：

```python
def purge_expired_ready() -> int:
    """回收超时未启动的 ready 任务：删原文件 + 标 failed。

    标 failed 而不是删 Task 行，是为了让用户在任务列表里看到发生了
    什么，而不是文件凭空消失。
    """
    cutoff = utcnow() - timedelta(hours=settings.ready_ttl_hours)
    reaped = 0
    with SessionLocal() as session:
        rows = (
            session.query(Task)
            .filter(Task.status == "ready", Task.updated_at < cutoff)
            .all()
        )
        for task in rows:
            drop_original(task.task_id)
            task.status = "failed"
            task.error_code = ReadyExpired.code
            task.error_message = (
                f"上传后 {settings.ready_ttl_hours} 小时内未开始转换，"
                "原文件已回收，请重新上传"
            )
            reaped += 1
        if reaped:
            session.commit()
    return reaped
```

**`NON_TERMINAL` 不要加 `ready`**——加了就会被 45 分钟的孤儿回收器标成 `TASK_ABANDONED`，那是错的诊断。

- [ ] **Step 6: 接线**

`main.py` 的 startup 钩子里，在 `purge_expired_shards()` 旁边加 `purge_expired_ready()`。

`pipeline.py` 的 `run_task` finally 里，在 `purge_expired_shards()` 旁边加同样一行（惰性触发，与既有两个清理保持一致）。

- [ ] **Step 7: 跑测试确认通过**

- [ ] **Step 8: 变异检查**

1. `purge_expired_ready` 的过滤条件 `Task.status == "ready"` 改成不过滤 → 预期 `test_non_ready_tasks_are_not_touched` 变红
2. `drop_original(task.task_id)` 删掉 → 预期 `test_expired_ready_task_is_reaped` 变红
3. `NON_TERMINAL` 加上 `"ready"` → 预期 `test_ready_is_not_touched_by_stale_reaper` 变红
4. `settings.ready_ttl_hours` 换成 `settings.upload_ttl_hours` → 预期有测试变红（若全绿，说明 TTL 的 monkeypatch 值设成了默认值，回去改）

- [ ] **Step 9: 补 `.env.example`**

在 `PPTX2PDF_UPLOAD_TTL_HOURS` 附近加：

```
# ready 任务（传完但没点「开始转换」）的原文件保留时长。
# 不要跟 UPLOAD_TTL_HOURS 混：那个管未完成的上传会话（支持断点续传），
# 调短它会让大文件传到一半、暂停超时后必须从头重传。
PPTX2PDF_READY_TTL_HOURS=1
```

- [ ] **Step 10: 跑全量并提交**

```bash
git add backend/app/config.py backend/app/errors.py backend/app/services/retention.py backend/app/main.py backend/app/services/pipeline.py backend/tests/test_retention_ready.py .env.example
git commit -m "feat(backend): ready 任务的 1 小时 TTL 回收"
```

---

## Task 6: 前端两段式上传

**Files:**
- Modify: `frontend/src/lib/api.ts`、`frontend/src/components/TaskCard.tsx`、`frontend/src/components/TaskCard.test.tsx`、`frontend/src/App.tsx`
- Create: `frontend/src/components/ReadyCard.tsx`、`frontend/src/components/ReadyCard.test.tsx`

**Interfaces:**
- Consumes: `POST /api/tasks/{id}/start`
- Produces: `startTask(taskId, engine?, options?) -> Promise<TaskDto>`；`ReadyCard` 组件

- [ ] **Step 1: 同步 `ready` 到三处**

**一处不改就会在运行时崩** —— 四期踩过：`STATUS[未知值]` 是 `undefined`，取 `.badge` 直接抛 `TypeError`，而仓库没有 ErrorBoundary，React 18 会卸载整棵树。

1. `api.ts` 的 `TaskDto['status']` 联合类型加 `'ready'`（现为 7 值，加后 8 值）
2. `TaskCard.tsx` 的 `STATUS` 映射表加 `ready` 条目，文案「待开始」，用中性色 `badge-neutral`
3. `useTaskPolling.ts` 的 `TERMINAL` —— **不要加 `ready`**，它不是终态（但也不需要轮询，见 Step 4）
4. `TaskCard.test.tsx:38` 的 `ALL_STATUSES` 数组加 `'ready'`（它是遍历全状态的守卫）

- [ ] **Step 2: 加 `startTask`**

`api.ts`：

```typescript
export async function startTask(
  taskId: string,
  engine?: EngineName,
  options?: ConversionOptions,
): Promise<TaskDto> {
  const resp = await fetch(`/api/tasks/${taskId}/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ engine, options }),
  })
  return parse<TaskDto>(resp)
}
```

- [ ] **Step 3: 写 `ReadyCard` 的失败测试**

创建 `frontend/src/components/ReadyCard.test.tsx`：

```tsx
it('显示文件名与体积', ...)
it('点「开始转换」调用 onStart 并带上当前选的引擎', ...)
it('开始转换请求进行中时禁用按钮', ...)   // 与四期自检按钮同理，避免被当成卡死
it('引擎切换后再点，带的是新引擎', ...)
```

- [ ] **Step 4: 实现 `ReadyCard`**

一个卡片，包含：文件名、体积、`EngineSelector`（复用 `App.tsx` 里现有的引擎选择组件）、`ConversionOptionsPanel`（同样复用）、「开始转换」按钮。

样式复用既有 `--c-*` 设计令牌与 `.card` 类，不新增视觉风格。

按钮在请求进行中 `disabled` 且文案变「启动中…」。

- [ ] **Step 5: 改 `App.tsx`**

`uploadFile` 返回后不再把 taskId 直接塞进 `taskIds` 开始轮询，改为存进新的 `readyTask` 状态并渲染 `ReadyCard`。用户点「开始转换」后调 `startTask`，成功再把 taskId 加进 `taskIds` 开始轮询。

四期那条「有风险时点确认前不发任何上传请求」的容量预判交互**保持不变**，只是决策点后移——用户在 `ReadyCard` 上换引擎时才需要重新评估。

- [ ] **Step 6: 跑前端测试与构建**

```
cd frontend && npm test -- --run && npm run build && npm run lint
```

- [ ] **Step 7: 变异检查**

1. `App.tsx` 里传完后直接开始轮询（跳过 ReadyCard）→ 预期有测试变红
2. `ReadyCard` 的按钮 `disabled` 删掉 → 预期「进行中禁用」那条变红
3. `startTask` 不传 `engine` → 预期「带上当前选的引擎」那条变红
4. `TaskCard.tsx` 的 `STATUS` 删掉 `ready` 条目 → 预期 `tsc` 报错（`Record<TaskDto['status'], ...>` 缺键）

- [ ] **Step 8: 提交**

```bash
git add frontend/src/lib/api.ts frontend/src/components/ReadyCard.tsx frontend/src/components/ReadyCard.test.tsx frontend/src/components/TaskCard.tsx frontend/src/components/TaskCard.test.tsx frontend/src/hooks/useTaskPolling.ts frontend/src/App.tsx
git commit -m "feat(frontend): 两段式上传，传完先停在就绪卡片"
```

---

## Task 7: openapi 快照与文档

**Files:**
- Modify: `backend/openapi.json`、`README.md`

- [ ] **Step 1: 重新生成快照**

```
cd backend && .venv/Scripts/python.exe -m scripts.dump_openapi
```

四期已经有 `tests/test_openapi_snapshot.py` 守着漂移，所以不重新生成的话它会变红。

- [ ] **Step 2: 跑测试确认快照一致**

```
.venv/Scripts/python.exe -m pytest tests/test_openapi_snapshot.py -q
```
预期：`1 passed`

- [ ] **Step 3: 补 README**

- 「关键配置」表加 `PPTX2PDF_READY_TTL_HOURS`（默认 1），说明它与 `UPLOAD_TTL_HOURS` 管的不是一回事
- 新增一小节说明两段式上传：传完停在就绪卡片，选好引擎和选项再点「开始转换」；一小时不点会被回收（`READY_EXPIRED`），需重新上传
- 「已知限制」加一条：内嵌视频/音频会在转换前被剥离且不可逆（PDF 本就放不了视频，信息损失为零，但服务器上不留原件，需要对照时得重传）

- [ ] **Step 4: 跑五条验收命令**

- [ ] **Step 5: 提交**

```bash
git add backend/openapi.json README.md
git commit -m "docs: 两段式上传与媒体剥离的说明；openapi 快照"
```

---

## 自审记录

**Spec 覆盖检查：**

| spec 章节 | 对应任务 |
|---|---|
| §3 媒体剥离（关系类型、复用切片机制、覆盖原件、空操作） | Task 1、2、3 |
| §4 两段式上传（ready 状态、start 端点、兜底挪动、前端三处同步） | Task 4、6 |
| §4 ready TTL 与独立配置项 | Task 5 |
| §6 API 契约（start 端点、`TASK_ALREADY_STARTED`、不复用 `TASK_NOT_READY`） | Task 4 |
| §7 测试策略与三条接线守护 | Task 3 Step 5、Task 4 Step 6、Task 6 Step 7 |
| §7 验收命令五条 | 每个任务末尾 + Task 7 Step 4 |
| §8 真机验收 | 计划外，由部署阶段执行 |

三条接线守护的落点：剥离被调用 → Task 3 Step 5-1；剥离取代原件 → Task 3 Step 5-3；`start` 才入队 → Task 4 Step 6-1。全部有对应的变异步骤。

**写计划时逐一 grep 核对过的事实**（四期六处疏漏里四处在测试脚手架，这次连测试侧引用也查了）：

- `pptx_split.py` 的六个候选函数**确认是纯 OPC 工具**，不含切片语义——读了 `_rels_path`/`_owner_part`/`_resolve`/`_read_rels`/`_rewrite_rels`/`_rewrite_content_types` 的实现
- 四个命名空间常量确认在 `pptx_split.py:16-19`（`REL_NS_URI`/`CT_NS_URI`/`REL_NS`/`CT_NS`，比我最初以为的多一个 `REL_NS`）
- `_resolve` 在 `pptx_split.py` 的第 76、220 行有调用，`_owner_part` 在第 316 行有调用——**不是只在被提取的函数内部使用**，所以 Task 1 的 import 六个全要，漏掉会 `NameError`
- `_collect`/`_slide_order`/`_rewrite_presentation` 是切片专有，不提取
- `test_pptx_split.py` **14 passed**，是提取的回归网
- `enqueue_conversion` 在 **`app/queue.py`**，不是 `app/services/queue.py`
- `TaskNotFound`/`TaskNotReady` 定义在 **`app/api/tasks.py`** 而非 `errors.py`
- **`TASK_NOT_READY` 已被 `download` 占用**（`tasks.py:110`「任务状态为 X，尚无可下载结果」），故新增 `TASK_ALREADY_STARTED`
- `TASK_ABANDONED` 存在于 `errors.py:97`
- `upload_ttl_hours: int = 24` 在 `config.py:16`
- `CompleteResponse` 只有 `task_id` 一个字段
- `ConversionOptions.expand_animations` 一期就存在（后端只落库不实现）
- 前端 `TERMINAL = new Set(['done','failed'])` 在 `useTaskPolling.ts:8`
- `ALL_STATUSES` 在 `TaskCard.test.tsx:38`，现列 7 个值
- 基线 293 / 71
