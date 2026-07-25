# 二期：LibreOffice 转换引擎 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把一期的占位 PDF 换成真实的 LibreOffice 转换，并一次做完容器化部署、队列、并发闸门、超时治理、资源保留策略与一期终审遗留的 8 项。

**Architecture:** docker-compose 三服务（api / worker / redis），文件走共享 volume 零拷贝，SQLite 移入 volume 开 WAL 供两个容器共享。转换由 RQ 投递到 worker 容器，worker 内调 `soffice --headless`，超时杀整个进程组，成功判定不看退出码而看输出文件的四条实证。

**Tech Stack:** Python 3.12 / FastAPI / RQ + Redis / SQLite(WAL) / LibreOffice(headless) / Docker Compose / 目标平台 ARM64

## Global Constraints

- **本期不写任何新测试代码。** 一期已有的 49 个后端测试与 19 个前端测试保留作回归网，每个任务改完必须跑通。功能验证在测试机上真跑，异常路径靠故障注入开关触发。
- 目标平台是 **ARM64 / aarch64**（Oracle Ampere A1，4 OCPU / 24 GB）。所有镜像与 apt 包必须有 arm64 构建。
- 开发机是 Windows 且**不装 LibreOffice**。本机只做：跑一期回归测试、`docker compose config` 语法校验、前端 `npm test` / `npm run build`。**不在本机构建镜像**（ARM 交叉构建过慢），Dockerfile 留到测试机验证。
- 所有 API 错误返回 `{"code": "...", "message": "..."}`，码值取自 `app/errors.py`。
- 后端所有路径操作用 `pathlib.Path`。
- `ChunkStore` 不得访问数据库；`pptx_probe` 不得访问 HTTP；`ConversionEngine` 不得访问数据库。
- 任务状态机固定 `pending → parsing → queued → converting → done`，任一环节可转 `failed`。
- 故障注入开关默认全部关闭，只在 `LibreOfficeEngine` 内部生效，不影响正常路径。
- 配置全部经 `Settings` 注入，`env_prefix="PPTX2PDF_"`。

**回归命令**（几乎每个任务的验证步骤都会用到）：
```bash
# 后端，工作目录 backend/
.venv/Scripts/python.exe -m pytest -q
# 期望：49 passed

# 前端，工作目录 frontend/
npm test && npm run build
# 期望：19 passed，build 成功
```

---

### Task 1: 配置项与 SQLite WAL

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/db.py`
- Create: `.env.example`

**Interfaces:**
- Consumes: 无
- Produces:
  - `settings.redis_url: str`、`output_ttl_hours: int`、`stale_task_minutes: int`、`convert_timeout_base_s: int`、`convert_timeout_per_slide_s: int`、`convert_timeout_max_s: int`、`soffice_bin: str`
  - `settings.debug_force_timeout / debug_force_engine_failure / debug_force_empty_output / debug_force_page_mismatch: bool`
  - 环境变量前缀 `PPTX2PDF_`

- [ ] **Step 1: 改写 config.py**

整个 `Settings` 类替换为（保留一期已有字段与 validator）：

```python
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
    convert_timeout_max_s: int = 1800
    soffice_bin: str = "soffice"

    # 二期新增：故障注入，默认全关
    debug_force_timeout: bool = False
    debug_force_engine_failure: bool = False
    debug_force_empty_output: bool = False
    debug_force_page_mismatch: bool = False

    @field_validator("storage_root")
    @classmethod
    def _resolve_storage_root(cls, value: Path) -> Path:
        return value.resolve()

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

**注意**：一期的 `_resolve_storage_root` validator 名称可能不同，以实际文件为准，行为（`.resolve()`）必须保留——一期终审专门修过 `output_path` 相对 CWD 的问题。

- [ ] **Step 2: 给 db.py 加 WAL pragma**

在 `backend/app/db.py` 的 `engine = create_engine(...)` 之后追加：

```python
from sqlalchemy import event


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _connection_record) -> None:
    """api 与 worker 两个容器共享同一个 SQLite 文件，必须开 WAL。

    WAL 允许一写多读并发；busy_timeout 让偶发的写锁竞争自动重试
    而不是立刻抛 database is locked。仅对 SQLite 生效。
    """
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()
```

`db.py` 顶部已经 `from app.config import settings`，无需新增 import。

- [ ] **Step 3: 写 .env.example**

```bash
# 复制成 .env 后按需修改。compose 会读取它。

# 并发度 = worker 容器副本数。4 核机器建议 2，留 1 核给 api 处理分片上传。
WORKER_REPLICAS=2

# ---- 转换超时（秒）----
# 实际超时 = min(max(BASE, 页数 × PER_SLIDE), MAX)
PPTX2PDF_CONVERT_TIMEOUT_BASE_S=180
PPTX2PDF_CONVERT_TIMEOUT_PER_SLIDE_S=4
PPTX2PDF_CONVERT_TIMEOUT_MAX_S=1800

# ---- 保留策略 ----
PPTX2PDF_OUTPUT_TTL_HOURS=24
PPTX2PDF_UPLOAD_TTL_HOURS=24
# 孤儿任务回收阈值，必须大于 CONVERT_TIMEOUT_MAX_S 换算的分钟数
PPTX2PDF_STALE_TASK_MINUTES=45

# ---- 故障注入，仅测试机排查用，默认全关 ----
# 把转换超时压到 1 秒，触发真实的进程组清理路径
PPTX2PDF_DEBUG_FORCE_TIMEOUT=false
# 引擎直接抛 ConversionFailed
PPTX2PDF_DEBUG_FORCE_ENGINE_FAILURE=false
# 转换后把输出截断为 0 字节，验证「退出码不可信」的检测
PPTX2PDF_DEBUG_FORCE_EMPTY_OUTPUT=false
# 转换后从输出 PDF 删掉一页，验证页数一致性检查
PPTX2PDF_DEBUG_FORCE_PAGE_MISMATCH=false
```

- [ ] **Step 4: 跑回归**

Run（工作目录 `backend/`）：`.venv/Scripts/python.exe -m pytest -q`
Expected: 49 passed

`env_prefix` 的加入不会影响测试——测试通过 `monkeypatch.setattr(settings, ...)` 直接改属性，不走环境变量。

- [ ] **Step 5: 提交**

```bash
git add backend/app/config.py backend/app/db.py .env.example
git commit -m "feat(backend): 二期配置项、env_prefix 与 SQLite WAL"
```

---

### Task 2: 错误族、引擎抽象与模型默认值

**Files:**
- Modify: `backend/app/errors.py`
- Modify: `backend/app/models.py`
- Modify: `backend/app/services/engines/base.py`
- Modify: `backend/app/services/engines/placeholder.py`
- Modify: `backend/requirements.txt`

**Interfaces:**
- Consumes: 无
- Produces:
  - `ConversionFailed`(`CONVERSION_FAILED`, 500)、`ConversionTimeout`(`CONVERSION_TIMEOUT`, 504)、`ConversionPageMismatch`(`CONVERSION_PAGE_MISMATCH`, 500)、`EngineUnavailable`(`ENGINE_UNAVAILABLE`, 503)、`ResultExpired`(`RESULT_EXPIRED`, 410)、`TaskAbandoned`(`TASK_ABANDONED`, 410)
  - `ConversionEngine.convert(src, meta, dest, *, timeout_s: float) -> None`
  - `Task.engine` 默认值 `"unassigned"`

- [ ] **Step 1: 给 errors.py 追加错误族**

在文件末尾追加：

```python
class ConversionFailed(AppError):
    code = "CONVERSION_FAILED"
    http_status = 500


class ConversionTimeout(AppError):
    code = "CONVERSION_TIMEOUT"
    http_status = 504


class ConversionPageMismatch(AppError):
    code = "CONVERSION_PAGE_MISMATCH"
    http_status = 500


class EngineUnavailable(AppError):
    code = "ENGINE_UNAVAILABLE"
    http_status = 503


class ResultExpired(AppError):
    code = "RESULT_EXPIRED"
    http_status = 410


class TaskAbandoned(AppError):
    code = "TASK_ABANDONED"
    http_status = 410
```

`TaskAbandoned` 不会被 raise，只用它的 `code` 常量给孤儿任务回收器（Task 7）落库，这样码值来源仍然统一在 `errors.py`。

- [ ] **Step 2: 改 Task.engine 的默认值**

`backend/app/models.py` 中：

```python
    engine: Mapped[str] = mapped_column(String(32), default="unassigned")
```

原值是 `"placeholder"`。改的原因：二期由 `run_task` 在 probe 之后才决定引擎，保留 `"placeholder"` 会让「引擎尚未分配」与「真的用了占位引擎」两种状态无法区分。

- [ ] **Step 3: 给 ConversionEngine 加 timeout_s**

`backend/app/services/engines/base.py`：

```python
    @abstractmethod
    def convert(self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float) -> None:
        """把 src 转成 PDF 写到 dest。失败时抛 AppError 子类。

        timeout_s 是单次转换的墙钟上限，实现方必须自己遵守它——
        调外部进程的实现要在超时后杀掉整个进程组，不能只杀父进程。
        """
```

`backend/app/services/engines/placeholder.py` 的签名同步改成：

```python
    def convert(self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float) -> None:
```

函数体不变（占位引擎瞬时完成，忽略 `timeout_s`）。

- [ ] **Step 4: 把 pypdf 移到生产依赖**

`backend/requirements.txt` 追加：
```
pypdf==5.1.0
rq==2.0.0
redis==5.2.1
```

`backend/requirements-dev.txt` 里已有的 `pypdf==5.1.0` 那一行删掉（它现在由 requirements.txt 提供，`requirements-dev.txt` 顶部的 `-r requirements.txt` 会带进来）。

原因：一期 pypdf 只在测试里用，二期 `LibreOfficeEngine` 要用它做输出验证，属于生产路径。

- [ ] **Step 5: 装依赖并跑回归**

Run（工作目录 `backend/`）：
```bash
.venv/Scripts/pip.exe install -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q
```
Expected: 49 passed

一期的 `test_placeholder_engine.py` 直接调 `PlaceholderEngine().convert(src, meta, dest)`——加了 keyword-only 参数后这些调用会 `TypeError`。**这是预期的破坏，必须修测试的调用方**：把 5 处调用改成 `convert(src, meta, dest, timeout_s=60)`。这是允许的改动，因为它是签名变更的机械适配，不是削弱断言。

- [ ] **Step 6: 提交**

```bash
git add backend/app/errors.py backend/app/models.py backend/app/services/engines/ backend/requirements.txt backend/requirements-dev.txt backend/tests/test_placeholder_engine.py
git commit -m "feat(backend): 转换错误族、引擎超时参数与 engine 默认值"
```

---

### Task 3: LibreOffice 引擎

**Files:**
- Create: `backend/app/services/engines/libreoffice.py`
- Modify: `backend/app/services/engines/__init__.py`

**Interfaces:**
- Consumes: `ConversionEngine`、`PptxMeta`、`ConversionFailed`、`ConversionTimeout`、`ConversionPageMismatch`、`EngineUnavailable`、`settings`
- Produces: `LibreOfficeEngine`（`name = "libreoffice"`），注册进 `_ENGINES`

- [ ] **Step 1: 写引擎实现**

`backend/app/services/engines/libreoffice.py`：

```python
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from app.config import settings
from app.errors import (
    ConversionFailed,
    ConversionPageMismatch,
    ConversionTimeout,
    EngineUnavailable,
)
from app.services.engines.base import ConversionEngine
from app.services.pptx_probe import PptxMeta

logger = logging.getLogger(__name__)

DEBUG_TIMEOUT_S = 1.0
LOG_TAIL_CHARS = 2000


class LibreOfficeEngine(ConversionEngine):
    """调 soffice --headless 做转换。

    三个必须遵守的约束，每一条都对应一类真实故障：
    1. 每次调用给独立的 UserInstallation 目录——多实例共用默认 profile
       会争抢锁文件，表现为随机失败或挂死。
    2. 超时必须杀整个进程组——soffice 会 fork 出真正干活的子进程，
       只杀父进程会留下孤儿继续吃内存，累积几个就打满机器。
    3. 退出码不可信——LibreOffice 转换失败时经常照样返回 0，
       成功判定必须以输出文件的实证为准。
    """

    name = "libreoffice"

    def convert(self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float) -> None:
        if settings.debug_force_engine_failure:
            raise ConversionFailed("故障注入 DEBUG_FORCE_ENGINE_FAILURE")

        # 把超时压到 1 秒，让真实的 soffice 调用超时，从而真正走一遍
        # killpg 清理路径——如果只是 sleep 再抛异常，测的就不是这段逻辑。
        effective_timeout = DEBUG_TIMEOUT_S if settings.debug_force_timeout else timeout_s

        dest.parent.mkdir(parents=True, exist_ok=True)
        profile = Path(tempfile.mkdtemp(prefix="lo_profile_"))
        cmd = [
            settings.soffice_bin,
            "--headless",
            "--norestore",
            "--invisible",
            f"-env:UserInstallation=file://{profile.as_posix()}",
            "--convert-to",
            "pdf:impress_pdf_Export",
            "--outdir",
            str(dest.parent),
            str(src),
        ]

        logger.info(
            "convert start src=%s slides=%d size=%d timeout=%.0fs cmd=%s",
            src.name, meta.slide_count, src.stat().st_size, effective_timeout, " ".join(cmd),
        )
        started = time.monotonic()

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 独立进程组，超时时整组端掉
            )
        except FileNotFoundError as exc:
            shutil.rmtree(profile, ignore_errors=True)
            raise EngineUnavailable(f"找不到可执行文件 {settings.soffice_bin}") from exc
        except OSError as exc:
            shutil.rmtree(profile, ignore_errors=True)
            raise EngineUnavailable(f"无法启动 soffice: {exc}") from exc

        try:
            stdout, _ = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            self._kill_process_group(proc)
            elapsed = time.monotonic() - started
            logger.warning(
                "convert timeout src=%s elapsed=%.1fs limit=%.0fs 已杀进程组",
                src.name, elapsed, effective_timeout,
            )
            raise ConversionTimeout(
                f"转换超时（{effective_timeout:.0f} 秒），文件可能过于复杂"
            ) from None
        finally:
            shutil.rmtree(profile, ignore_errors=True)

        elapsed = time.monotonic() - started
        logger.info(
            "convert finished src=%s rc=%s elapsed=%.1fs soffice_output=%s",
            src.name, proc.returncode, elapsed,
            stdout.decode("utf-8", errors="replace")[:LOG_TAIL_CHARS],
        )

        self._apply_output_fault_injection(dest)
        self._verify_output(dest, meta, src)

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen) -> None:
        """杀掉整个进程组并回收。父进程可能已退出，各种竞态都要吞掉。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.error("进程组 %s 在 SIGKILL 后仍未退出", proc.pid)

    @staticmethod
    def _apply_output_fault_injection(dest: Path) -> None:
        if not dest.is_file():
            return
        if settings.debug_force_empty_output:
            dest.write_bytes(b"")
            logger.warning("故障注入 DEBUG_FORCE_EMPTY_OUTPUT：输出已截断为 0 字节")
        elif settings.debug_force_page_mismatch:
            reader = PdfReader(str(dest))
            writer = PdfWriter()
            for page in reader.pages[:-1]:
                writer.add_page(page)
            with dest.open("wb") as fh:
                writer.write(fh)
            logger.warning("故障注入 DEBUG_FORCE_PAGE_MISMATCH：输出已删掉最后一页")

    @staticmethod
    def _verify_output(dest: Path, meta: PptxMeta, src: Path) -> None:
        """成功判定的四条实证。任一不成立即失败，且删掉坏文件。"""
        if not dest.is_file():
            raise ConversionFailed(
                f"soffice 未产出输出文件（退出码不可信，以文件为准）: {dest.name}"
            )
        if dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise ConversionFailed("输出 PDF 为 0 字节")
        try:
            pages = len(PdfReader(str(dest)).pages)
        except Exception as exc:
            dest.unlink(missing_ok=True)
            raise ConversionFailed(f"输出 PDF 无法解析: {exc}") from exc
        if pages != meta.slide_count:
            dest.unlink(missing_ok=True)
            raise ConversionPageMismatch(
                f"页数不符：{src.name} 有 {meta.slide_count} 页，"
                f"转出的 PDF 只有 {pages} 页"
            )
        logger.info("convert verified dest=%s pages=%d size=%d", dest.name, pages, dest.stat().st_size)
```

- [ ] **Step 2: 注册进引擎表**

`backend/app/services/engines/__init__.py` 改为：

```python
from app.services.engines.base import ConversionEngine
from app.services.engines.libreoffice import LibreOfficeEngine
from app.services.engines.placeholder import PlaceholderEngine

_ENGINES: dict[str, type[ConversionEngine]] = {
    PlaceholderEngine.name: PlaceholderEngine,
    LibreOfficeEngine.name: LibreOfficeEngine,
}


def get_engine(name: str) -> ConversionEngine:
    """按名取引擎。存类而非实例，构造推迟到真正使用时。"""
    return _ENGINES[name]()


__all__ = ["ConversionEngine", "LibreOfficeEngine", "PlaceholderEngine", "get_engine"]
```

一期终审已把 `_ENGINES` 从存实例改成存类，这里沿用——`LibreOfficeEngine` 的构造不做任何探测，但保持惰性构造的形状，三期加 `GraphEngine`（要建鉴权客户端）时才不会在 import 期崩掉整个包。

- [ ] **Step 3: 跑回归**

Run（工作目录 `backend/`）：`.venv/Scripts/python.exe -m pytest -q`
Expected: 49 passed

本机没有 soffice，但 `LibreOfficeEngine` 只在被调用时才启动进程，import 本身无副作用，一期测试不受影响。

- [ ] **Step 4: 提交**

```bash
git add backend/app/services/engines/
git commit -m "feat(backend): LibreOffice 引擎，含进程组清理与输出实证校验"
```

---

### Task 4: 引擎路由、资源保留与流水线接线

**Files:**
- Create: `backend/app/services/engine_router.py`
- Create: `backend/app/services/retention.py`
- Modify: `backend/app/services/pipeline.py`

**Interfaces:**
- Consumes: `PptxMeta`、`settings`、`get_engine`
- Produces:
  - `select_engine(meta: PptxMeta) -> str`
  - `compute_timeout_s(slide_count: int) -> float`
  - `drop_original(task_id: str) -> None`
  - `purge_expired_outputs() -> int`（返回删除数量）

这三个文件必须在同一个任务里完成：`pipeline.py` 同时 import 前两者，拆开会留下一个 import 不存在模块的中间提交，审查时跑不了测试。

- [ ] **Step 1: 写引擎路由**

`backend/app/services/engine_router.py`：

```python
from app.services.pptx_probe import PptxMeta


def select_engine(meta: PptxMeta) -> str:
    """按元信息选转换引擎。

    二期只有一个真引擎，恒返回 libreoffice。这个函数存在的意义是
    把选择点固定在 probe 之后——三期 Graph 的判据是页数
    （Graph 有 100 页硬上限），而页数要解析完才知道。
    选择逻辑留在 HTTP 层的话，三期得回头改 uploads.py。
    """
    return "libreoffice"
```

- [ ] **Step 2: 写保留策略**

`backend/app/services/retention.py`：

```python
import logging
import time

from app.config import settings

logger = logging.getLogger(__name__)


def drop_original(task_id: str) -> None:
    """转换结束后删掉原始 pptx，不论成败。

    用户要的是 PDF；失败了他会重传，留着原文件也没有诊断价值。
    真实文件是 80MB–500MB 量级，不删的话磁盘增长速度翻倍。
    """
    path = settings.originals_dir / f"{task_id}.pptx"
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删除原文件失败 %s: %s", path, exc)


def purge_expired_outputs() -> int:
    """惰性清理过期的输出 PDF，返回删除数量。

    每次任务结束后顺带扫一次，与一期 _purge_expired 同一模式。
    长期没有新任务时不清理，但那也意味着磁盘没在增长，所以惰性成立，
    不需要额外的 cron 容器。
    """
    cutoff = time.time() - settings.output_ttl_hours * 3600
    removed = 0
    try:
        candidates = list(settings.outputs_dir.glob("*.pdf"))
    except OSError as exc:
        logger.warning("扫描输出目录失败: %s", exc)
        return 0

    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            logger.warning("删除过期输出失败 %s: %s", path, exc)
    return removed
```

- [ ] **Step 3: 改写 pipeline.py**

整个文件替换为：

```python
import json
import logging
from datetime import datetime, timezone

from app.config import settings
from app.db import SessionLocal
from app.errors import AppError
from app.models import Task
from app.services.engine_router import select_engine
from app.services.engines import get_engine
from app.services.pptx_probe import probe
from app.services.retention import drop_original, purge_expired_outputs

logger = logging.getLogger(__name__)


def compute_timeout_s(slide_count: int) -> float:
    """按页数算转换超时。

    固定值行不通：10 页与 500 页的合理耗时差一个数量级，
    定小了卡死大文件，定大了让僵死的任务占着 worker 不放。
    """
    return float(
        min(
            max(
                settings.convert_timeout_base_s,
                slide_count * settings.convert_timeout_per_slide_s,
            ),
            settings.convert_timeout_max_s,
        )
    )


def _set_status(session, task: Task, status: str) -> None:
    task.status = status
    session.commit()


def _record_failure(session, task_id: str, code: str, message: str) -> None:
    """失败落库自身也可能失败——回滚后用干净会话重试一次，仍失败则记日志，绝不再抛。"""
    try:
        session.rollback()
        task = session.get(Task, task_id)
        if task is not None:
            task.error_code = code
            task.error_message = message
            task.status = "failed"
            session.commit()
    except Exception:
        logger.exception("无法记录任务 %s 的失败状态", task_id)


def run_task(task_id: str) -> None:
    """走完整状态机：parsing → queued → converting → done / failed。

    这个函数由 RQ worker 在独立子进程里执行，不再是 FastAPI 的
    BackgroundTasks。签名保持只吃 task_id，自开 session。
    """
    session = SessionLocal()
    started = datetime.now(timezone.utc)
    try:
        task = session.get(Task, task_id)
        if task is None:
            logger.warning("run_task 收到不存在的 task_id=%s", task_id)
            return

        src = settings.originals_dir / f"{task_id}.pptx"
        dest = settings.outputs_dir / f"{task_id}.pdf"
        logger.info("task start id=%s file=%s size=%d", task_id, task.original_filename, task.size_bytes)

        try:
            _set_status(session, task, "parsing")
            meta = probe(src)
            task.slide_count = meta.slide_count
            task.slide_width_emu = meta.slide_width_emu
            task.slide_height_emu = meta.slide_height_emu
            task.fonts_json = json.dumps(list(meta.fonts), ensure_ascii=False)
            task.engine = select_engine(meta)
            logger.info(
                "task parsed id=%s slides=%d engine=%s fonts=%s",
                task_id, meta.slide_count, task.engine, list(meta.fonts)[:20],
            )

            _set_status(session, task, "queued")
            _set_status(session, task, "converting")

            timeout_s = compute_timeout_s(meta.slide_count)
            get_engine(task.engine).convert(src, meta, dest, timeout_s=timeout_s)

            task.output_path = str(dest.resolve())
            _set_status(session, task, "done")
            logger.info(
                "task done id=%s elapsed=%.1fs output=%d bytes",
                task_id,
                (datetime.now(timezone.utc) - started).total_seconds(),
                dest.stat().st_size,
            )
        except AppError as exc:
            logger.warning("task failed id=%s code=%s msg=%s", task_id, exc.code, exc.message)
            _record_failure(session, task_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001  后台任务兜底，异常静默会让任务永久卡住
            logger.exception("task crashed id=%s", task_id)
            _record_failure(session, task_id, "INTERNAL_ERROR", str(exc))
    finally:
        # 原文件转换结束即删，不论成败——用户要的是 PDF，
        # 失败了他会重传，留着诊断也用不上。这砍掉一半的磁盘增长。
        drop_original(task_id)
        removed = purge_expired_outputs()
        if removed:
            logger.info("retention 清理了 %d 个过期输出", removed)
        session.close()
```

- [ ] **Step 4: 跑回归**

Run（工作目录 `backend/`）：`.venv/Scripts/python.exe -m pytest -q`
Expected: 49 passed

一期测试会真实走到这条流水线（占位引擎已被 `select_engine` 换成 `libreoffice`）——**这会让一期的 `test_tasks_api.py` 和 `test_e2e_large_upload.py` 失败**，因为本机没有 soffice，转换会抛 `EngineUnavailable`，任务落到 `failed` 而非 `done`。

这是预期的行为变更，按下面方式适配（属于签名/行为变更的机械适配，不是削弱断言）：在 `backend/tests/conftest.py` 追加一个 autouse fixture，把测试环境的引擎选择固定回占位引擎：

```python
@pytest.fixture(autouse=True)
def _force_placeholder_engine(monkeypatch):
    """本机没有 LibreOffice。一期测试验证的是上传协议与状态机，
    不是转换质量——把引擎选择固定回占位引擎，让这些测试继续有效。
    真实转换在测试机上验证，见计划的完成判据。"""
    monkeypatch.setattr(
        "app.services.pipeline.select_engine", lambda meta: "placeholder"
    )
```

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/engine_router.py backend/app/services/retention.py backend/app/services/pipeline.py backend/tests/conftest.py
git commit -m "feat(backend): 引擎路由、资源保留与流水线接线"
```

---

### Task 5: 下载端点的结果过期处理

**Files:**
- Modify: `backend/app/api/tasks.py`

**Interfaces:**
- Consumes: `settings`、`ResultExpired`
- Produces: `download` 端点在结果被保留策略清理后返回 `RESULT_EXPIRED`(410)

- [ ] **Step 1: 让 download 区分「未就绪」与「已过期」**

`backend/app/api/tasks.py` 的 `download` 函数改为：

```python
@router.get("/{task_id}/download")
def download(task_id: str, session: Session = Depends(get_session)) -> FileResponse:
    task = _load(session, task_id)
    if task.status != "done" or not task.output_path:
        raise TaskNotReady(f"任务状态为 {task.status}，尚无可下载结果")

    path = Path(task.output_path)
    if not path.is_file():
        # 任务确实成功过，但结果已被保留策略清理——这与「还没转完」
        # 是两回事，前端要据此提示用户重新上传而不是继续等。
        raise ResultExpired(
            f"结果文件已超过 {settings.output_ttl_hours} 小时保留期被清理，请重新上传"
        )

    stem = Path(task.original_filename).stem
    return FileResponse(str(path), media_type="application/pdf", filename=f"{stem}.pdf")
```

文件顶部相应调整 import：加入 `from app.config import settings` 和 `from app.errors import ResultExpired`。一期为处理「文件不存在」而复用 `TaskNotReady` 的那段逻辑被这段取代。

- [ ] **Step 2: 跑回归**

Run（工作目录 `backend/`）：`.venv/Scripts/python.exe -m pytest -q`
Expected: 49 passed

一期 `test_tasks_api.py` 里有一个断言「done 之前下载返回 409」的用例，`TaskNotReady` 路径未变，仍应通过。

**注意**：一期终审时实现者曾主动加过一个「`output_path` 文件不存在时复用 `TaskNotReady`」的防御分支，本任务正是把它替换成语义准确的 `ResultExpired`。别把两条路径都留着——文件不存在只有「已过期被清理」这一种成因，因为 `run_task` 只在转换成功后才写 `output_path`。

- [ ] **Step 3: 提交**

```bash
git add backend/app/api/tasks.py
git commit -m "feat(backend): 结果过期返回 RESULT_EXPIRED 而非 TASK_NOT_READY"
```

---

### Task 6: RQ 队列与 worker 入口

**Files:**
- Create: `backend/app/queue.py`
- Create: `backend/worker.py`
- Modify: `backend/app/api/uploads.py`

**Interfaces:**
- Consumes: `settings`、`run_task`
- Produces:
  - `get_queue() -> rq.Queue`
  - `enqueue_conversion(task_id: str) -> None`
  - `backend/worker.py` 可执行入口

- [ ] **Step 1: 写队列封装**

`backend/app/queue.py`：

```python
import logging

from redis import Redis
from rq import Queue

from app.config import settings

logger = logging.getLogger(__name__)

QUEUE_NAME = "conversions"
# RQ 层的超时只是兜底，防 job 在 subprocess 之外的地方卡住。
# 引擎内部的 subprocess 超时（按页数算）才是主超时，会先触发，
# 那条路径我们能优雅处理：杀进程组、清 profile、落 CONVERSION_TIMEOUT。
JOB_TIMEOUT_MARGIN_S = 60


def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=Redis.from_url(settings.redis_url))


def enqueue_conversion(task_id: str) -> None:
    """把转换任务投进队列。

    job_timeout 用配置的最大值而非按页数算——enqueue 时还没 probe，
    不知道页数。这只是兜底上限，真正的超时在引擎内部。
    """
    from app.services.pipeline import run_task

    job_timeout = settings.convert_timeout_max_s + JOB_TIMEOUT_MARGIN_S
    job = get_queue().enqueue(run_task, task_id, job_timeout=job_timeout)
    logger.info("enqueued task=%s job=%s timeout=%ds", task_id, job.id, job_timeout)
```

**不传 `retry=` 参数是有意的**：RQ 默认不重试，这正是我们要的。LibreOffice 转不动的文件重试还是转不动，只白占几分钟 CPU；`run_task` 内部已经把失败落成 `failed` 状态，重试反而会覆盖掉已记录的错误码。唯一该重来的情况是 worker 进程被 kill（部署、OOM），那种情况 RQ 自己会把 job 放回队列，不需要 `retry` 配置。

`run_task` 用函数内 import：`pipeline` 会 import 引擎、引擎 import pypdf，放在模块顶层会让 api 容器也背上这些依赖的导入成本，而 api 从不执行转换。

- [ ] **Step 2: 写 worker 入口**

`backend/worker.py`：

```python
"""RQ worker 入口。容器 CMD 直接跑这个文件。

每个 job 由 RQ fork 出独立子进程执行，soffice 崩溃、内存泄漏、
段错误都被隔离在那个子进程里，不累积到常驻 worker 上。
"""

import logging
import sys

from redis import Redis
from rq import Worker

from app.config import settings
from app.db import init_db
from app.queue import QUEUE_NAME

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("worker")


def main() -> None:
    settings.ensure_dirs()
    init_db()

    for flag in (
        "debug_force_timeout",
        "debug_force_engine_failure",
        "debug_force_empty_output",
        "debug_force_page_mismatch",
    ):
        if getattr(settings, flag):
            logger.warning("故障注入开关已开启: PPTX2PDF_%s —— 生产环境请关闭", flag.upper())

    logger.info(
        "worker 启动 redis=%s storage=%s soffice=%s",
        settings.redis_url, settings.storage_root, settings.soffice_bin,
    )
    Worker([QUEUE_NAME], connection=Redis.from_url(settings.redis_url)).work()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 把 uploads.py 的 BackgroundTasks 换成 enqueue**

`backend/app/api/uploads.py` 的 `complete_upload`：

1. 函数签名去掉 `background: BackgroundTasks` 参数
2. 文件顶部去掉 `BackgroundTasks` 的 import，加上 `from app.queue import enqueue_conversion`
3. 去掉 `from app.services.pipeline import run_task` 这行 import
4. 创建 `Task` 时**不再**传 `engine=` 参数（用模型默认的 `"unassigned"`）
5. 函数末尾的 `background.add_task(run_task, task_id)` 换成 `enqueue_conversion(task_id)`

改完后 `complete_upload` 的结尾部分应是：

```python
    upload.status = "completed"
    task = Task(
        task_id=task_id,
        upload_id=upload_id,
        original_filename=upload.filename,
        size_bytes=upload.size_bytes,
        status="pending",
    )
    session.add(task)
    session.commit()

    store().purge(upload_id)
    enqueue_conversion(task_id)
    return CompleteResponse(task_id=task_id)
```

- [ ] **Step 4: 让一期测试能在没有 Redis 的情况下跑**

一期的 `test_uploads_api.py` 和 `test_tasks_api.py` 依赖「complete 之后任务同步跑完」这个行为——BackgroundTasks 在 TestClient 里是同步执行的，换成 RQ 后没有 Redis 就会连接失败。

在 `backend/tests/conftest.py` 追加一个 autouse fixture，把 `enqueue_conversion` 替换成直接同步调用：

```python
@pytest.fixture(autouse=True)
def _sync_conversion(monkeypatch):
    """测试环境没有 Redis。把入队换成同步执行，保持一期测试对
    「complete 之后任务已跑完」的假设成立。"""
    from app.services.pipeline import run_task

    monkeypatch.setattr("app.api.uploads.enqueue_conversion", run_task)
```

这不是削弱测试——一期这些测试验证的是上传协议与任务状态机，不是队列投递机制；队列本身按 Global Constraints 不写测试，在测试机上真跑验证。

- [ ] **Step 5: 跑回归**

Run（工作目录 `backend/`）：`.venv/Scripts/python.exe -m pytest -q`
Expected: 49 passed

- [ ] **Step 6: 提交**

```bash
git add backend/app/queue.py backend/worker.py backend/app/api/uploads.py backend/tests/conftest.py
git commit -m "feat(backend): RQ 队列、worker 入口，替换 BackgroundTasks"
```

---

### Task 7: 孤儿任务回收与分片流式读取

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/uploads.py`

**Interfaces:**
- Consumes: `settings`、`Task`、`SessionLocal`、`TaskAbandoned`
- Produces: `reap_stale_tasks() -> int`

- [ ] **Step 1: 写孤儿任务回收器**

`backend/app/main.py` 中加入（并在 startup 里调用）：

```python
import logging
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.errors import TaskAbandoned
from app.models import Task

logger = logging.getLogger(__name__)

NON_TERMINAL = ("pending", "parsing", "queued", "converting")


def reap_stale_tasks() -> int:
    """把卡在非终态太久的任务标为失败，返回回收数量。

    RQ 的 job 只活在 worker 进程里。进程被 kill（部署、OOM、崩溃）
    时任务会永远停在中间态，而前端按异步轮询设计，会一直轮询一个
    永不改变的状态。上游设计 §10.3 定的方向是显性要求用户重传。

    时间比较用 naive UTC：SQLAlchemy 的 SQLite dialect 落库时会丢掉
    时区信息，读回来是 naive，拿 aware datetime 去比会 TypeError。
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=settings.stale_task_minutes
    )
    session = SessionLocal()
    try:
        stale = (
            session.query(Task)
            .filter(Task.status.in_(NON_TERMINAL), Task.updated_at < cutoff)
            .all()
        )
        for task in stale:
            task.status = "failed"
            task.error_code = TaskAbandoned.code
            task.error_message = "任务在服务重启前未完成，请重新上传"
        if stale:
            session.commit()
            logger.warning("回收了 %d 个孤儿任务", len(stale))
        return len(stale)
    except Exception:
        logger.exception("回收孤儿任务失败")
        session.rollback()
        return 0
    finally:
        session.close()
```

startup 事件体内在 `init_db()` 之后追加 `reap_stale_tasks()`。同时把日志配置放进 `main.py` 顶部（在 `app = FastAPI(...)` 之前），与 worker 保持一致：

```python
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    stream=sys.stdout,
)
```

`import sys` 必须一并加入 `main.py` 的 import 段——一期没有用到它。日志走 stdout 是为了让 `docker compose logs` 能收集到；二期的验收全靠看日志。

- [ ] **Step 2: put_chunk 改流式读取**

`backend/app/api/uploads.py` 的 `put_chunk`，把「先 `await request.body()` 再校验」改成边读边计数：

```python
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > upload.chunk_size:
        raise UploadSizeExceeded(
            f"块 {index} 声明 {declared} 字节，超过块大小 {upload.chunk_size}"
        )

    # 流式读取并在超限时立即中断。Content-Length 缺失时
    # （Transfer-Encoding: chunked）await request.body() 是无上限的，
    # 校验发生在整个 body already 进内存之后，起不到防护作用。
    buffer = bytearray()
    async for part in request.stream():
        buffer.extend(part)
        if len(buffer) > upload.chunk_size:
            raise UploadSizeExceeded(
                f"块 {index} 实际超过块大小 {upload.chunk_size} 字节"
            )
    data = bytes(buffer)
```

后续 `chunks.save_chunk(upload_id, index, data)` 那部分不变。

- [ ] **Step 3: 跑回归**

Run（工作目录 `backend/`）：`.venv/Scripts/python.exe -m pytest -q`
Expected: 49 passed

一期有两个测试专门验证块大小校验（Content-Length 预检与读后复验），流式改造后仍应通过。

- [ ] **Step 4: 提交**

```bash
git add backend/app/main.py backend/app/api/uploads.py
git commit -m "feat(backend): 孤儿任务回收与分片流式读取"
```

---

### Task 8: OpenAPI 契约补全业务错误码

**Files:**
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/api/uploads.py`
- Modify: `backend/app/api/tasks.py`
- Modify: `backend/openapi.json`（由脚本重新生成）

**Interfaces:**
- Consumes: 无
- Produces: `ErrorResponse` schema、各路由的 `responses=` 声明

- [ ] **Step 1: 定义错误响应模型**

`backend/app/schemas.py` 追加：

```python
class ErrorResponse(BaseModel):
    """所有 API 错误的统一形状，码值取自 app/errors.py。"""

    code: str
    message: str
```

- [ ] **Step 2: 给上传路由加 responses 声明**

`backend/app/api/uploads.py` 顶部加入：

```python
from app.schemas import ErrorResponse

_ERR = {"model": ErrorResponse}
UPLOAD_ERRORS = {
    404: {**_ERR, "description": "UPLOAD_SESSION_NOT_FOUND"},
    409: {**_ERR, "description": "UPLOAD_INCOMPLETE / UPLOAD_SESSION_NOT_ACTIVE"},
    410: {**_ERR, "description": "UPLOAD_SESSION_EXPIRED"},
    413: {**_ERR, "description": "UPLOAD_SIZE_EXCEEDED"},
    422: {**_ERR, "description": "VALIDATION_ERROR / UPLOAD_CHECKSUM_MISMATCH / PPTX_*"},
    507: {**_ERR, "description": "STORAGE_FULL"},
}
```

四个路由装饰器各加 `responses=UPLOAD_ERRORS`，例如：

```python
@router.post("", response_model=CreateUploadResponse, responses=UPLOAD_ERRORS)
```

- [ ] **Step 3: 给任务路由加 responses 声明**

`backend/app/api/tasks.py` 顶部加入：

```python
from app.schemas import ErrorResponse

_ERR = {"model": ErrorResponse}
TASK_ERRORS = {
    404: {**_ERR, "description": "TASK_NOT_FOUND"},
    409: {**_ERR, "description": "TASK_NOT_READY"},
    410: {**_ERR, "description": "RESULT_EXPIRED"},
}
```

两个路由装饰器各加 `responses=TASK_ERRORS`。

- [ ] **Step 4: 重新生成契约快照**

Run（工作目录 `backend/`）：
```bash
.venv/Scripts/python.exe -m scripts.dump_openapi
git diff --stat backend/openapi.json
```
Expected: `openapi.json` 有实质变化，`components.schemas` 中出现 `ErrorResponse`，各端点的 `responses` 中出现 404/409/410/413/422/507。

- [ ] **Step 5: 跑回归**

Run（工作目录 `backend/`）：`.venv/Scripts/python.exe -m pytest -q`
Expected: 49 passed

- [ ] **Step 6: 提交**

```bash
git add backend/app/schemas.py backend/app/api/ backend/openapi.json
git commit -m "feat(backend): 业务错误码补进 OpenAPI 契约"
```

---

### Task 9: 前端轮询上限

**Files:**
- Modify: `frontend/src/hooks/useTaskPolling.ts`
- Modify: `frontend/src/components/TaskCard.tsx`

**Interfaces:**
- Consumes: `TaskDto`、`getTask`
- Produces: `useTaskPolling(taskId: string): { task: TaskDto | null; pollingTimedOut: boolean }`

- [ ] **Step 1: 给轮询加总时长上限**

`frontend/src/hooks/useTaskPolling.ts` 整个替换为：

```ts
import { useEffect, useState } from 'react'
import { getTask, type TaskDto } from '../lib/api'

const POLL_MS = 1000
const TERMINAL = new Set(['done', 'failed'])
// 必须大于后端的 STALE_TASK_MINUTES（默认 45 分钟），
// 否则前端会在后端把孤儿任务标 failed 之前就先放弃。
const MAX_POLL_MS = 46 * 60 * 1000

export interface TaskPollingState {
  task: TaskDto | null
  pollingTimedOut: boolean
}

export function useTaskPolling(taskId: string): TaskPollingState {
  const [task, setTask] = useState<TaskDto | null>(null)
  const [pollingTimedOut, setPollingTimedOut] = useState(false)

  useEffect(() => {
    let cancelled = false
    let timer: number
    const startedAt = Date.now()

    setTask(null)
    setPollingTimedOut(false)

    const poll = async () => {
      if (Date.now() - startedAt > MAX_POLL_MS) {
        if (!cancelled) setPollingTimedOut(true)
        return
      }
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

  return { task, pollingTimedOut }
}
```

- [ ] **Step 2: 让 TaskCard 消费新签名**

`frontend/src/components/TaskCard.tsx` 中：

```tsx
  const { task, pollingTimedOut } = useTaskPolling(taskId)

  if (pollingTimedOut) {
    return (
      <div className="glass" style={{ padding: 16 }}>
        <p role="alert" style={{ color: 'var(--g-danger)', margin: 0 }}>
          任务状态长时间未更新，可能已中断。请重新上传。
        </p>
      </div>
    )
  }

  if (!task) {
    return (
      <div className="glass" style={{ padding: 16 }}>
        载入中…
      </div>
    )
  }
```

原来的 `const task = useTaskPolling(taskId)` 那一行被上面第一行取代，`if (!task)` 之前插入超时分支。其余部分不变。

- [ ] **Step 3: 跑前端回归**

Run（工作目录 `frontend/`）：
```bash
npm test
npm run build
```
Expected: 19 passed，build 成功

- [ ] **Step 4: 提交**

```bash
git add frontend/src/hooks/useTaskPolling.ts frontend/src/components/TaskCard.tsx
git commit -m "feat(frontend): 轮询总时长上限与中断提示"
```

---

### Task 10: 容器化

**Files:**
- Create: `deploy/api.Dockerfile`
- Create: `deploy/worker.Dockerfile`
- Create: `deploy/worker-entrypoint.sh`
- Create: `deploy/fontconfig-local.conf`
- Create: `docker-compose.yml`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `backend/worker.py`、`backend/app/main.py`
- Produces: 三服务的 compose 编排

- [ ] **Step 1: 写 api 镜像**

`deploy/api.Dockerfile`：

```dockerfile
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: 写 worker 镜像**

`deploy/worker.Dockerfile`：

```dockerfile
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# libreoffice-impress 带起 Impress 的 PDF 导出过滤器。
# 字体是保真度的 80%：Carlito/Caladea/Liberation 与微软字体 metric 兼容，
# 换行位置不变；Noto CJK 保证中文不渲染成豆腐块；Noto core 覆盖希腊语
# （公式里的希腊字母）；STIX 顶替 Cambria Math。
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-impress \
      libreoffice-core \
      fontconfig \
      fonts-crosextra-carlito \
      fonts-crosextra-caladea \
      fonts-liberation \
      fonts-liberation2 \
      fonts-noto-cjk \
      fonts-noto-core \
      fonts-dejavu-core \
      fonts-stix \
 && rm -rf /var/lib/apt/lists/*

COPY deploy/fontconfig-local.conf /etc/fonts/local.conf
RUN mkdir -p /usr/share/fonts/truetype/extra

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY deploy/worker-entrypoint.sh /usr/local/bin/worker-entrypoint.sh
RUN chmod +x /usr/local/bin/worker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/worker-entrypoint.sh"]
CMD ["python", "worker.py"]
```

- [ ] **Step 3: 写 entrypoint**

`deploy/worker-entrypoint.sh`：

```sh
#!/bin/sh
set -e

# 用户可能把自己的 Office 字体挂载到 /usr/share/fonts/truetype/extra，
# 挂载发生在镜像构建之后，所以字体缓存必须在容器启动时重建。
fc-cache -f >/dev/null 2>&1 || true

exec "$@"
```

**注意**：这个文件必须是 LF 换行。Windows 上用 CRLF 保存会让容器报 `exec format error`。仓库根的 `.gitattributes` 若不存在则新建，加入一行 `*.sh text eol=lf`。

- [ ] **Step 4: 写 fontconfig 映射**

`deploy/fontconfig-local.conf`：

```xml
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <!--
    把 Office 中文字体映射到 Noto CJK。

    关键：用 mode="append" binding="weak" 而不是 assign/strong。
    append+weak 是「加一个 fallback」——如果用户把真正的等线/雅黑
    挂载进 /usr/share/fonts/truetype/extra，真字体优先命中，
    这里的映射不会覆盖它。用 assign 会强制替换掉用户挂载的字体。

    metric 说明：Carlito/Caladea/Liberation 与 Calibri/Cambria/Arial
    是 metric 兼容的（字宽逐字符对齐，换行位置不变），fontconfig
    自带的规则已经处理，无需在此重复。中文没有 metric 兼容的自由
    替代，所以中文段落的换行位置仍会偏移，这是已知边界。
  -->
  <match target="pattern">
    <test name="family"><string>DengXian</string></test>
    <edit name="family" mode="append" binding="weak"><string>Noto Sans CJK SC</string></edit>
  </match>
  <match target="pattern">
    <test name="family"><string>等线</string></test>
    <edit name="family" mode="append" binding="weak"><string>Noto Sans CJK SC</string></edit>
  </match>
  <match target="pattern">
    <test name="family"><string>Microsoft YaHei</string></test>
    <edit name="family" mode="append" binding="weak"><string>Noto Sans CJK SC</string></edit>
  </match>
  <match target="pattern">
    <test name="family"><string>微软雅黑</string></test>
    <edit name="family" mode="append" binding="weak"><string>Noto Sans CJK SC</string></edit>
  </match>
  <match target="pattern">
    <test name="family"><string>SimSun</string></test>
    <edit name="family" mode="append" binding="weak"><string>Noto Serif CJK SC</string></edit>
  </match>
  <match target="pattern">
    <test name="family"><string>宋体</string></test>
    <edit name="family" mode="append" binding="weak"><string>Noto Serif CJK SC</string></edit>
  </match>
  <match target="pattern">
    <test name="family"><string>Cambria Math</string></test>
    <edit name="family" mode="append" binding="weak"><string>STIX Two Math</string></edit>
  </match>
</fontconfig>
```

- [ ] **Step 5: 写 compose 编排**

`docker-compose.yml`（仓库根）：

```yaml
services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  api:
    build:
      context: .
      dockerfile: deploy/api.Dockerfile
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - storage:/app/storage
    env_file:
      - .env
    environment:
      PPTX2PDF_STORAGE_ROOT: /app/storage
      PPTX2PDF_DATABASE_URL: sqlite:////app/storage/pptx2pdf.db
      PPTX2PDF_REDIS_URL: redis://redis:6379/0
    depends_on:
      redis:
        condition: service_healthy

  worker:
    build:
      context: .
      dockerfile: deploy/worker.Dockerfile
    restart: unless-stopped
    deploy:
      replicas: ${WORKER_REPLICAS:-2}
      resources:
        limits:
          memory: 3G
    volumes:
      - storage:/app/storage
      - ./fonts-extra:/usr/share/fonts/truetype/extra:ro
    env_file:
      - .env
    environment:
      PPTX2PDF_STORAGE_ROOT: /app/storage
      PPTX2PDF_DATABASE_URL: sqlite:////app/storage/pptx2pdf.db
      PPTX2PDF_REDIS_URL: redis://redis:6379/0
    depends_on:
      redis:
        condition: service_healthy

volumes:
  storage:
```

`DATABASE_URL` 的四条斜杠是绝对路径写法（`sqlite:////app/...` = `sqlite:///` + `/app/...`），少一条会变成相对 CWD，api 和 worker 会各写各的库。

- [ ] **Step 6: 更新 .gitignore**

追加：

```
fonts-extra/
.env
```

- [ ] **Step 7: 校验 compose 语法**

Run（仓库根）：
```bash
cp .env.example .env
mkdir -p fonts-extra
docker compose config -q
```
Expected: 无输出即通过。**不要跑 `docker compose build`**——目标平台是 ARM64，在 Windows 开发机上交叉构建过慢，镜像构建留到测试机。

- [ ] **Step 8: 提交**

```bash
git add deploy/ docker-compose.yml .gitignore .gitattributes
git commit -m "feat(deploy): 容器化编排、LibreOffice 镜像与字体配置"
```

---

### Task 11: 部署文档

**Files:**
- Modify: `deploy/nginx.conf.example`
- Modify: `README.md`

**Interfaces:**
- Consumes: 前十个任务的全部产物
- Produces: 可照抄执行的部署步骤

- [ ] **Step 1: 修正 nginx 样例**

`deploy/nginx.conf.example` 中，把 `listen 443 ssl http2;` 改为两行并补证书指令：

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name pptx.example.com;

    # 证书路径按实际部署修改。缺这两行 nginx 起不来。
    ssl_certificate     /etc/letsencrypt/live/pptx.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pptx.example.com/privkey.pem;

    # 分片上传后单次请求体只有 5 MiB，无需为 500MB 放宽。
    # 留 16m 余量覆盖块加请求头。改大它没有好处，只会削弱防护。
    client_max_body_size 16m;
    client_body_timeout 120s;
    ...
}
```

`listen 443 ssl http2;` 的写法在 nginx ≥1.25.1 已废弃，会打 warning；`http2 on;` 是新语法。其余部分（`location /`、`location /api/` 的 proxy 配置）不变。

- [ ] **Step 2: 重写 README**

`README.md` 整体替换为：

```markdown
# pptx → PDF

把课程 pptx 转成能直接导入 GoodNotes / OneNote 的 PDF。

**当前进度：二期（LibreOffice 引擎）。** 一期的占位 PDF 已被真实转换取代。

## 保真度边界

pptx 只存字体名不存字形，字体缺失时渲染端会替换，替换字体的字符宽度不同
导致换行位置改变——这是排版错位的根因。

- **西文接近完美**：镜像内置 Carlito / Caladea / Liberation，与 Calibri /
  Cambria / Arial 是 metric 兼容的，换行位置不变。
- **中文仍有偏差**：等线、微软雅黑受版权保护不能打进镜像，且没有 metric
  兼容的自由替代。镜像装的是 Noto CJK，保证中文不渲染成豆腐块，但中文段落
  的换行位置会偏移。

要消除中文偏差，把你自己 Windows 的 `C:\Windows\Fonts` 里的等线、微软雅黑
拷进宿主机的 `fonts-extra/` 目录，容器启动时会自动加载并优先使用。

## 部署

需要 Docker 与 docker-compose。目标平台 ARM64 或 x86_64 均可。

```bash
git clone https://github.com/MrZhongzq/pptxToPdf.git
cd pptxToPdf

cp .env.example .env      # 按需修改，默认值适合 4 核 24GB 的机器
mkdir -p fonts-extra      # 可选：把 Office 字体放进去

docker compose up -d --build
docker compose logs -f
```

前端需要单独构建并交给 nginx 托管：

```bash
cd frontend && npm ci && npm run build
# 产物在 frontend/dist/，参考 deploy/nginx.conf.example
```

### 关键配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `WORKER_REPLICAS` | 2 | 并发转换数。4 核机器建议 2，留 1 核给上传 |
| `PPTX2PDF_CONVERT_TIMEOUT_PER_SLIDE_S` | 4 | 每页超时系数。ARM 机器偏慢，转换总超时 = `min(max(180, 页数×4), 1800)` 秒 |
| `PPTX2PDF_OUTPUT_TTL_HOURS` | 24 | 输出 PDF 保留时长，过期自动清理 |
| `PPTX2PDF_STALE_TASK_MINUTES` | 45 | 孤儿任务回收阈值，必须大于最大转换超时 |

### 排查：故障注入开关

异常路径不容易自然触发，用这些开关主动跑一遍。默认全关，改完 `.env`
后 `docker compose up -d` 重启生效，worker 启动时会打 WARNING 提醒。

| 变量 | 触发什么 |
|---|---|
| `PPTX2PDF_DEBUG_FORCE_TIMEOUT` | 把超时压到 1 秒，验证进程组是否被杀干净、profile 是否清理 |
| `PPTX2PDF_DEBUG_FORCE_ENGINE_FAILURE` | 引擎直接失败，验证失败落库与前端错误展示 |
| `PPTX2PDF_DEBUG_FORCE_EMPTY_OUTPUT` | 输出截断为 0 字节，验证「退出码不可信」的检测 |
| `PPTX2PDF_DEBUG_FORCE_PAGE_MISMATCH` | 输出删掉一页，验证页数一致性检查 |

## 开发

后端（一期的 49 个测试是回归网，二期不新增测试）：

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # Linux 用 .venv/bin/
.venv/Scripts/python -m pytest -q                   # 期望 49 passed
```

前端：

```bash
cd frontend
npm install
npm test          # 期望 19 passed
npm run dev       # 开发服务器在 5173，/api 已代理到 8000
```

本机不需要装 LibreOffice——转换只在 worker 容器里跑。

## 已知限制

- 无鉴权、无配额，任何人都能上传 600MB（四期才做）
- UI 没有断点续传入口：客户端库和后端协议都支持，但按设计刷新即重置
- 任务列表只在 React state，刷新即丢，且没有列表端点可恢复
- SQLite 靠 WAL 支撑 api 与 worker 两个容器共享，**不要把 storage volume
  挂到 NFS 或对象存储 FUSE 上**，那种场景下文件锁不可靠
- 前端产物需要单独构建部署，没有打进 compose

## 分期

| 期 | 内容 | 状态 |
|---|---|---|
| 一 | 前端三端 UI + 分片上传全链路 + 元信息解析 + 占位 PDF | 完成 |
| 二 | LibreOffice 引擎 + 容器化 + 队列 + 资源治理 | 进行中 |
| 三 | Microsoft Graph 引擎（小文件高保真）+ 转换切片合并 | 未开始 |
| 四 | 账号、配额、风控、管理面板 | 未开始 |

设计文档见 `docs/superpowers/specs/`，实施计划见 `docs/superpowers/plans/`。
```

- [ ] **Step 3: 跑完整回归**

Run：
```bash
cd backend && .venv/Scripts/python.exe -m pytest -q
cd ../frontend && npm test && npm run build
cd .. && docker compose config -q
```
Expected: 后端 49 passed，前端 19 passed，build 成功，compose 校验通过

- [ ] **Step 4: 提交**

```bash
git add deploy/nginx.conf.example README.md
git commit -m "docs: 二期部署说明、保真度边界与故障注入开关"
```

---

## 二期完成判据

代码层面（本机可验证）：

- [ ] 后端 `pytest -q` 49 passed
- [ ] 前端 `npm test` 19 passed，`npm run build` 成功
- [ ] `docker compose config -q` 通过
- [ ] `backend/openapi.json` 已更新并提交，包含全部业务错误码

测试机验证（本机做不了，代码写完后进行）：

- [ ] `docker compose up -d --build` 三服务全部起来，`docker compose logs` 无异常
- [ ] 上传一份真实课程 pptx，转换成功，下载的 PDF 页数正确、**文本可选中**
- [ ] 中文课件转换后中文不是豆腐块
- [ ] 把 Office 字体放进 `fonts-extra/` 重启后，字体确实被优先使用
- [ ] 四个故障注入开关各拨一次，日志中确认对应分支被执行、任务状态正确落 `failed`
- [ ] `FORCE_TIMEOUT` 那次结束后 `ps aux | grep soffice` 无残留进程
- [ ] 转换完成后 `originals/` 下对应文件已被删除
- [ ] 同时上传多个文件，确认并发数不超过 `WORKER_REPLICAS`
