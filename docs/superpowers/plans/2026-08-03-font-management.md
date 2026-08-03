# 字体管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** admin 面板里能查看、上传、删除字体，上传与已有字体撞名时显式列出冲突候选由管理员逐个决策。

**Architecture:** 字体文件存 `/app/storage/fonts/`（api 与 worker 共享的 storage 卷），fontconfig 通过 `<dir>` 认这个目录，写入后立即生效。不建数据库表——文件系统是唯一真相来源，元信息靠 `fc-query` 现扫。上传拆成 preflight（落临时目录 + 返回冲突候选）与 commit（凭 token 落盘）两步。

**Tech Stack:** FastAPI + fontconfig 的 `fc-query`（镜像自带，不引入 fontTools）+ React。

## Global Constraints

- 设计稿：`docs/superpowers/specs/2026-08-03-font-management-design.md`，有冲突以设计稿为准。
- **不新建数据库表、不新增 Python 依赖。**
- 冲突匹配一律只看 `family`，**不看 `style`**——各家命名不统一（DejaVu 的常规体 style 是 `Book` 不是 `Regular`）。
- 冲突判定粒度是**文件**不是 face：`.ttc` 一个文件含多个 face，替换和删除只能整个文件来。
- 单文件上限 **64 MB**，只接受 `.ttf` / `.ttc` / `.otf`。
- 临时文件 TTL **30 分钟**。
- 所有端点挂 `require_admin`。
- 后端注释与用户可见文案用中文，与现有代码一致。
- 每个任务结束时后端跑 `cd backend && python -m pytest -q`，前端跑 `cd frontend && npx vitest run`，全绿才提交。

---

### Task 1: fc-query 输出解析

**Files:**
- Create: `backend/app/services/font_probe.py`
- Test: `backend/tests/test_font_probe.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) FontFace: family: str, style: str, version: str, index: int`
  - `parse_fc_query(stdout: str) -> list[FontFace]`
  - `parse_charset(raw: str) -> int`
  - `format_version(raw: str) -> str`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_font_probe.py`：

```python
"""fc-query 输出的解析。

用固定的样本输出做单测，不依赖真实字体文件——真字体体积大且有版权。
样本取自真机上 fontconfig 2.14 的实际输出。
"""

from app.services.font_probe import FontFace, format_version, parse_charset, parse_fc_query


def test_parses_single_face_ttf() -> None:
    stdout = "DejaVu Sans\tBook\t155320\t0\n"
    assert parse_fc_query(stdout) == [
        FontFace(family="DejaVu Sans", style="Book", version="2.37", index=0)
    ]


def test_parses_every_face_of_a_ttc() -> None:
    """ttc 是字体集合，一个文件多个 face。微软雅黑与 Noto CJK 都是这种。

    冲突判定要看全部 face，漏一个就会出现「明明撞了却没提示」。
    """
    stdout = (
        "Noto Sans CJK JP\tBold\t131072\t0\n"
        "Noto Sans CJK KR\tBold\t131072\t1\n"
        "Noto Sans CJK SC\tBold\t131072\t2\n"
    )
    faces = parse_fc_query(stdout)
    assert [f.family for f in faces] == [
        "Noto Sans CJK JP",
        "Noto Sans CJK KR",
        "Noto Sans CJK SC",
    ]
    assert [f.index for f in faces] == [0, 1, 2]


def test_ignores_blank_and_malformed_lines() -> None:
    """fc-query 偶尔会在末尾多输出空行；字段不足的行直接跳过而不是抛错,
    否则一个畸形 face 会让整个文件不可用。"""
    stdout = "DejaVu Sans\tBook\t155320\t0\n\n坏行\n"
    assert len(parse_fc_query(stdout)) == 1


def test_version_is_fixed_point_divided_by_65536() -> None:
    """fontversion 是 16.16 定点数，直接显示会变成 155320 这种天书。"""
    assert format_version("155320") == "2.37"
    assert format_version("131072") == "2.00"


def test_version_falls_back_to_dash_when_unparsable() -> None:
    assert format_version("") == "-"
    assert format_version("abc") == "-"


def test_counts_codepoints_from_charset_ranges() -> None:
    """charset 是十六进制范围列表，空格分隔，每项是 start-end 或单个码位。

    这个数字比版本号有用得多：版本号看不出子集化，字数能。
    """
    # 20-7e = 95 个, a0 = 1 个, 300-302 = 3 个
    assert parse_charset("20-7e a0 300-302") == 99


def test_empty_charset_counts_zero() -> None:
    assert parse_charset("") == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_font_probe.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.services.font_probe'`

- [ ] **Step 3: 写实现**

创建 `backend/app/services/font_probe.py`：

```python
"""从字体文件里读出 family / style / 版本 / 覆盖字数。

用 fontconfig 自带的 fc-query，不引入 fontTools——镜像里已经装了
fontconfig（LibreOffice 依赖它），多一个 Python 依赖就多一份构建风险。

本模块只做纯解析，不碰文件系统，方便单测。
"""

from __future__ import annotations

from dataclasses import dataclass

#: fc-query 的输出格式。字段用 tab 分隔——family 里可能有空格（"DejaVu Sans"），
#: 用空格分隔会切错。
FC_QUERY_FORMAT = "%{family}\t%{style}\t%{fontversion}\t%{index}\n"


@dataclass(frozen=True)
class FontFace:
    """字体文件里的一个 face。

    ttc 一个文件含多个 face，所以 face 与文件是多对一。
    """

    family: str
    style: str
    version: str
    index: int


def format_version(raw: str) -> str:
    """fontversion 是 16.16 定点数，除以 65536 才是人看的版本号。

    155320 → "2.37"。解析不了就给一个横杠，不要让整个列表崩掉——
    有些字体的这个字段是空的。
    """
    try:
        return f"{int(raw) / 65536:.2f}"
    except (TypeError, ValueError):
        return "-"


def parse_fc_query(stdout: str) -> list[FontFace]:
    """把 fc-query 的多行输出解析成 face 列表。

    字段不足的行直接跳过而不是抛错：一个畸形 face 不该让整个文件不可用。
    """
    faces: list[FontFace] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        family, style, version, index = parts[0], parts[1], parts[2], parts[3]
        if not family.strip():
            continue
        try:
            idx = int(index)
        except ValueError:
            idx = 0
        faces.append(
            FontFace(
                family=family.strip(),
                style=style.strip(),
                version=format_version(version),
                index=idx,
            )
        )
    return faces


def parse_charset(raw: str) -> int:
    """数出 charset 覆盖多少个码位。

    输入形如 "20-7e a0-2e9 2f3"，十六进制，空格分隔，每项是范围或单点。
    看不懂的项跳过——宁可少算几个字，也不要因为一个畸形范围就报错。
    """
    total = 0
    for token in raw.split():
        try:
            if "-" in token:
                start, end = token.split("-", 1)
                total += int(end, 16) - int(start, 16) + 1
            else:
                int(token, 16)
                total += 1
        except ValueError:
            continue
    return total
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_font_probe.py -q`
Expected: PASS，7 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/font_probe.py backend/tests/test_font_probe.py
git commit -m "feat(fonts): fc-query 输出解析

ttc 一个文件含多个 face（微软雅黑与 Noto CJK 都是），所以解析必须
返回全部 face——漏一个就会出现「明明撞了却没提示」。

fontversion 是 16.16 定点数，直接显示是 155320 这种天书，要除 65536。
charset 的码位数比版本号有用：版本号看不出子集化，字数能。"
```

---

### Task 2: 字体目录扫描与冲突判定

**Files:**
- Create: `backend/app/services/font_store.py`
- Test: `backend/tests/test_font_store.py`

**Interfaces:**
- Consumes: `FontFace`, `parse_fc_query`, `parse_charset`, `FC_QUERY_FORMAT` from Task 1
- Produces:
  - `@dataclass(frozen=True) FontFile: file_id: str, filename: str, source: str, faces: tuple[FontFace, ...], size_bytes: int, charset_count: int, sha256: str, modified_at: datetime`
  - `SOURCE_MANAGED = "managed"` / `SOURCE_MOUNTED = "mounted"` / `SOURCE_BUILTIN = "builtin"`
  - `encode_file_id(source: str, filename: str) -> str`
  - `decode_file_id(file_id: str) -> tuple[str, str]`
  - `safe_filename(raw: str) -> str`
  - `resolve_collision(directory: Path, filename: str) -> str`
  - `find_conflicts(incoming: FontFile, existing: list[FontFile]) -> list[FontFile]`
  - `is_duplicate(incoming_sha: str, existing: list[FontFile]) -> FontFile | None`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_font_store.py`：

```python
"""字体目录的扫描、文件名安全处理与冲突判定。

冲突判定是纯函数，不碰文件系统，所以这里的用例全部用构造的 FontFile。
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.font_probe import FontFace
from app.services.font_store import (
    SOURCE_BUILTIN,
    SOURCE_MANAGED,
    SOURCE_MOUNTED,
    FontFile,
    decode_file_id,
    encode_file_id,
    find_conflicts,
    is_duplicate,
    resolve_collision,
    safe_filename,
)


def _font(filename: str, families: list[str], *, sha: str = "aa", source: str = SOURCE_MANAGED) -> FontFile:
    return FontFile(
        file_id=encode_file_id(source, filename),
        filename=filename,
        source=source,
        faces=tuple(FontFace(family=f, style="Regular", version="1.00", index=i)
                    for i, f in enumerate(families)),
        size_bytes=1024,
        charset_count=100,
        sha256=sha,
        modified_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )


class TestFileId:
    def test_round_trips(self) -> None:
        fid = encode_file_id(SOURCE_MANAGED, "msyh.ttc")
        assert decode_file_id(fid) == (SOURCE_MANAGED, "msyh.ttc")

    def test_survives_non_ascii_and_slashes_in_name(self) -> None:
        """中文文件名与看起来像路径的名字都要能原样还原，
        否则删除时会找不到文件。"""
        fid = encode_file_id(SOURCE_MANAGED, "思源黑体 Regular.otf")
        assert decode_file_id(fid) == (SOURCE_MANAGED, "思源黑体 Regular.otf")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            decode_file_id("!!!not-base64!!!")


class TestSafeFilename:
    def test_strips_directory_components(self) -> None:
        """路径穿越：写进字体目录之外是绝对不能发生的。"""
        assert safe_filename("../../etc/passwd") == "passwd"
        assert safe_filename("/abs/path/font.ttf") == "font.ttf"
        assert safe_filename("..\\\\windows\\\\font.ttf") == "font.ttf"

    def test_replaces_illegal_characters(self) -> None:
        assert safe_filename('a<b>c:d"e|f?g*.ttf') == "a_b_c_d_e_f_g_.ttf"

    def test_keeps_chinese_and_spaces(self) -> None:
        assert safe_filename("思源黑体 Regular.otf") == "思源黑体 Regular.otf"

    def test_falls_back_when_name_becomes_empty(self) -> None:
        assert safe_filename("...") == "font"
        assert safe_filename("") == "font"


class TestResolveCollision:
    def test_returns_name_unchanged_when_free(self, tmp_path: Path) -> None:
        assert resolve_collision(tmp_path, "a.ttf") == "a.ttf"

    def test_appends_incrementing_suffix_before_extension(self, tmp_path: Path) -> None:
        (tmp_path / "a.ttf").write_bytes(b"x")
        assert resolve_collision(tmp_path, "a.ttf") == "a-2.ttf"
        (tmp_path / "a-2.ttf").write_bytes(b"x")
        assert resolve_collision(tmp_path, "a.ttf") == "a-3.ttf"


class TestDuplicate:
    def test_same_sha_is_a_duplicate(self) -> None:
        existing = [_font("old.ttf", ["A"], sha="deadbeef")]
        assert is_duplicate("deadbeef", existing).filename == "old.ttf"

    def test_different_sha_is_not(self) -> None:
        existing = [_font("old.ttf", ["A"], sha="deadbeef")]
        assert is_duplicate("cafe", existing) is None


class TestFindConflicts:
    def test_matches_on_family_regardless_of_style(self) -> None:
        """style 各家命名不统一，只能看 family。"""
        incoming = _font("new.ttf", ["微软雅黑"])
        existing = [_font("old.ttf", ["微软雅黑"])]
        assert [c.filename for c in find_conflicts(incoming, existing)] == ["old.ttf"]

    def test_any_face_matching_makes_the_whole_file_a_candidate(self) -> None:
        """ttc 的任一 face 撞了，整个文件就是候选——替换只能整文件来。"""
        incoming = _font("new.ttc", ["Noto Sans CJK JP", "Noto Sans CJK SC"])
        existing = [_font("old.ttc", ["完全无关", "Noto Sans CJK SC"])]
        assert len(find_conflicts(incoming, existing)) == 1

    def test_no_overlap_means_no_conflict(self) -> None:
        incoming = _font("new.ttf", ["A"])
        existing = [_font("old.ttf", ["B"])]
        assert find_conflicts(incoming, existing) == []

    def test_includes_mounted_and_builtin_as_candidates(self) -> None:
        """手工挂载与内置的也要列出来——它们删不掉，但管理员需要知道
        名字被占了、自己传的可能不生效。"""
        incoming = _font("new.ttf", ["微软雅黑"])
        existing = [
            _font("m.ttf", ["微软雅黑"], source=SOURCE_MOUNTED),
            _font("b.ttf", ["微软雅黑"], source=SOURCE_BUILTIN),
        ]
        assert len(find_conflicts(incoming, existing)) == 2

    def test_does_not_report_the_file_against_itself(self) -> None:
        """同一个文件（file_id 相同）不算与自己冲突。"""
        incoming = _font("same.ttf", ["A"])
        assert find_conflicts(incoming, [incoming]) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_font_store.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.services.font_store'`

- [ ] **Step 3: 写实现**

创建 `backend/app/services/font_store.py`：

```python
"""字体目录的扫描、写入、删除与冲突判定。

三个来源：
- managed  /app/storage/fonts              面板管理，可删
- mounted  /usr/share/fonts/truetype/extra 手工挂载，只读（容器里是 :ro）
- builtin  其余系统字体目录                镜像内置，只读

**不建数据库表**：文件系统就是真相来源。删掉文件列表自动一致，不会出现
「DB 有记录、文件已不在」的孤儿态。代价是每次列表都要扫目录，managed
通常十几个文件，可接受；builtin 有 300+ 个，由调用方决定要不要扫。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.services.font_probe import (
    FC_QUERY_FORMAT,
    FontFace,
    parse_charset,
    parse_fc_query,
)

SOURCE_MANAGED = "managed"
SOURCE_MOUNTED = "mounted"
SOURCE_BUILTIN = "builtin"

#: 只有 managed 的字体允许删除或被替换。
WRITABLE_SOURCES = frozenset({SOURCE_MANAGED})

ALLOWED_SUFFIXES = frozenset({".ttf", ".ttc", ".otf"})

MOUNTED_DIR = Path("/usr/share/fonts/truetype/extra")
BUILTIN_DIRS = (Path("/usr/share/fonts"), Path("/usr/local/share/fonts"))

#: Windows 与 POSIX 都不接受的字符，外加控制字符。
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class FontFile:
    file_id: str
    filename: str
    source: str
    faces: tuple[FontFace, ...]
    size_bytes: int
    charset_count: int
    sha256: str
    modified_at: datetime

    @property
    def families(self) -> tuple[str, ...]:
        """去重后的 family 列表，保持出现顺序。"""
        seen: dict[str, None] = {}
        for f in self.faces:
            seen.setdefault(f.family, None)
        return tuple(seen)

    @property
    def deletable(self) -> bool:
        return self.source in WRITABLE_SOURCES


def encode_file_id(source: str, filename: str) -> str:
    """把来源与文件名编成一个 URL-safe 的 id。

    没有数据库表，所以没有自增主键可用。base64 而不是直接拼字符串：
    文件名里可能有中文、空格、斜杠，直接放进 URL 路径会被解析器切错。
    """
    raw = f"{source}/{filename}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_file_id(file_id: str) -> tuple[str, str]:
    padding = "=" * (-len(file_id) % 4)
    try:
        raw = base64.urlsafe_b64decode(file_id + padding).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"无法解析的字体 id: {file_id}") from exc
    source, _, filename = raw.partition("/")
    if not source or not filename:
        raise ValueError(f"无法解析的字体 id: {file_id}")
    return source, filename


def safe_filename(raw: str) -> str:
    """把上传来的文件名收拾干净。

    路径穿越是这里的重点：写进字体目录之外绝对不能发生。先取 basename
    （同时处理 / 与 \\，因为上传方可能是 Windows），再替换非法字符。
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = _ILLEGAL.sub("_", name).strip()
    if not name.strip(". "):
        return "font"
    return name


def resolve_collision(directory: Path, filename: str) -> str:
    """重名时在扩展名前加 -2 / -3 递增后缀。"""
    if not (directory / filename).exists():
        return filename
    stem, dot, suffix = filename.rpartition(".")
    if not dot:
        stem, suffix = filename, ""
    n = 2
    while True:
        candidate = f"{stem}-{n}{dot}{suffix}" if dot else f"{stem}-{n}"
        if not (directory / candidate).exists():
            return candidate
        n += 1


def probe(path: Path, source: str) -> FontFile | None:
    """读一个字体文件的元信息。不是字体就返回 None。"""
    try:
        meta = subprocess.run(
            ["fc-query", "--format", FC_QUERY_FORMAT, str(path)],
            capture_output=True, text=True, timeout=30,
        )
        charset = subprocess.run(
            ["fc-query", "--format", "%{charset}", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if meta.returncode != 0:
        return None
    faces = parse_fc_query(meta.stdout)
    if not faces:
        return None
    stat = path.stat()
    return FontFile(
        file_id=encode_file_id(source, path.name),
        filename=path.name,
        source=source,
        faces=tuple(faces),
        size_bytes=stat.st_size,
        charset_count=parse_charset(charset.stdout if charset.returncode == 0 else ""),
        sha256=sha256_of(path),
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def scan_dir(directory: Path, source: str) -> list[FontFile]:
    """扫一个目录下的字体。目录不存在就返回空列表——
    fonts-extra 在没挂载时确实可能不存在，那不是错误。"""
    if not directory.is_dir():
        return []
    found: list[FontFile] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        info = probe(path, source)
        if info is not None:
            found.append(info)
    return found


def is_duplicate(incoming_sha: str, existing: list[FontFile]) -> FontFile | None:
    """完全相同的文件已经存在——没有歧义，直接拦，不打扰管理员。"""
    for f in existing:
        if f.sha256 == incoming_sha:
            return f
    return None


def find_conflicts(incoming: FontFile, existing: list[FontFile]) -> list[FontFile]:
    """找出会与 incoming 抢同一个 family 的已有文件。

    只看 family 不看 style：各家 style 命名不统一。任一 face 撞了整个
    文件就是候选，因为替换与删除只能整文件来（ttc 含多个 face）。

    手工挂载与内置的也会列出来。它们删不掉，但管理员需要知道名字被占了。
    """
    wanted = set(incoming.families)
    return [
        f for f in existing
        if f.file_id != incoming.file_id and wanted.intersection(f.families)
    ]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_font_store.py -q`
Expected: PASS，17 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/font_store.py backend/tests/test_font_store.py
git commit -m "feat(fonts): 目录扫描、文件名安全处理与冲突判定

冲突只看 family 不看 style——各家命名不统一，DejaVu 的常规体 style
是 Book 不是 Regular。任一 face 撞了整个文件就是候选，因为替换与删除
只能整文件来。

file_id 用 base64 而不是直接拼文件名：中文、空格、斜杠放进 URL 路径
会被解析器切错。"
```

---

### Task 3: 列表与删除端点

**Files:**
- Create: `backend/app/api/admin_fonts.py`
- Modify: `backend/app/main.py`（注册路由，约 155 行附近）
- Modify: `backend/app/errors.py`（追加错误类）
- Modify: `backend/app/schemas.py`（追加 DTO）
- Modify: `backend/app/config.py`（追加字体目录配置）
- Test: `backend/tests/test_admin_fonts.py`

**Interfaces:**
- Consumes: `FontFile`, `scan_dir`, `decode_file_id`, `SOURCE_*`, `MOUNTED_DIR`, `BUILTIN_DIRS` from Task 2
- Produces:
  - `GET /api/admin/fonts?include_builtin=false` → `FontListDto`
  - `DELETE /api/admin/fonts/{file_id}` → 204
  - `settings.font_dir: Path`、`settings.font_tmp_dir: Path`
  - `FontNotFound`、`FontNotDeletable`、`FontInvalid`、`FontTooLarge`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_admin_fonts.py`：

```python
"""字体管理端点。

字体文件是二进制且有版权，测试里不放真字体：把 font_store.scan_dir
换成桩，只验端点的鉴权、分组、删除语义。解析本身在 test_font_probe.py
与 test_font_store.py 里覆盖。
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.font_probe import FontFace
from app.services.font_store import (
    SOURCE_BUILTIN,
    SOURCE_MANAGED,
    SOURCE_MOUNTED,
    FontFile,
    encode_file_id,
)


def _font(filename: str, source: str, family: str = "微软雅黑") -> FontFile:
    return FontFile(
        file_id=encode_file_id(source, filename),
        filename=filename,
        source=source,
        faces=(FontFace(family=family, style="Regular", version="6.30", index=0),),
        size_bytes=19_000_000,
        charset_count=28_762,
        sha256="a" * 64,
        modified_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class TestAuth:
    def test_list_requires_admin(self, client: TestClient) -> None:
        assert client.get("/api/admin/fonts").status_code in (401, 403)

    def test_delete_requires_admin(self, client: TestClient) -> None:
        fid = encode_file_id(SOURCE_MANAGED, "a.ttf")
        assert client.delete(f"/api/admin/fonts/{fid}").status_code in (401, 403)


class TestList:
    def test_groups_by_source(self, admin_session: TestClient, monkeypatch) -> None:
        import app.api.admin_fonts as mod

        monkeypatch.setattr(mod, "_scan_all", lambda include_builtin: [
            _font("managed.ttf", SOURCE_MANAGED),
            _font("mounted.ttf", SOURCE_MOUNTED),
            _font("builtin.ttf", SOURCE_BUILTIN),
        ])
        body = admin_session.get("/api/admin/fonts").json()
        assert [f["filename"] for f in body["managed"]] == ["managed.ttf"]
        assert [f["filename"] for f in body["mounted"]] == ["mounted.ttf"]
        assert [f["filename"] for f in body["builtin"]] == ["builtin.ttf"]

    def test_only_managed_is_deletable(self, admin_session: TestClient, monkeypatch) -> None:
        import app.api.admin_fonts as mod

        monkeypatch.setattr(mod, "_scan_all", lambda include_builtin: [
            _font("managed.ttf", SOURCE_MANAGED),
            _font("mounted.ttf", SOURCE_MOUNTED),
        ])
        body = admin_session.get("/api/admin/fonts").json()
        assert body["managed"][0]["deletable"] is True
        assert body["mounted"][0]["deletable"] is False

    def test_builtin_skipped_by_default(self, admin_session: TestClient, monkeypatch) -> None:
        """内置有 300+ 个，扫一次要秒级，默认不扫。"""
        import app.api.admin_fonts as mod

        seen = {}
        def fake(include_builtin):
            seen["flag"] = include_builtin
            return []
        monkeypatch.setattr(mod, "_scan_all", fake)
        admin_session.get("/api/admin/fonts")
        assert seen["flag"] is False
        admin_session.get("/api/admin/fonts?include_builtin=true")
        assert seen["flag"] is True

    def test_exposes_families_and_readable_version(self, admin_session: TestClient, monkeypatch) -> None:
        import app.api.admin_fonts as mod

        monkeypatch.setattr(mod, "_scan_all", lambda include_builtin: [
            _font("msyh.ttc", SOURCE_MANAGED)
        ])
        item = admin_session.get("/api/admin/fonts").json()["managed"][0]
        assert item["families"] == ["微软雅黑"]
        assert item["version"] == "6.30"
        assert item["charset_count"] == 28_762


class TestDelete:
    def test_removes_a_managed_file(self, admin_session: TestClient, tmp_path, monkeypatch) -> None:
        import app.api.admin_fonts as mod
        from app.config import settings

        monkeypatch.setattr(settings, "font_dir", tmp_path)
        target = tmp_path / "a.ttf"
        target.write_bytes(b"x")

        resp = admin_session.delete(f"/api/admin/fonts/{encode_file_id(SOURCE_MANAGED, 'a.ttf')}")

        assert resp.status_code == 204
        assert not target.exists()

    def test_refuses_to_delete_mounted(self, admin_session: TestClient) -> None:
        """手工挂载的目录是 :ro，删了也会失败——提前拦住并说明原因，
        比让 OSError 冒成 500 强。"""
        fid = encode_file_id(SOURCE_MOUNTED, "a.ttf")
        resp = admin_session.delete(f"/api/admin/fonts/{fid}")
        assert resp.status_code == 400
        assert resp.json()["code"] == "FONT_NOT_DELETABLE"

    def test_missing_file_is_404_not_silent_success(self, admin_session: TestClient, tmp_path, monkeypatch) -> None:
        """静默成功会让管理员以为删掉了。"""
        from app.config import settings

        monkeypatch.setattr(settings, "font_dir", tmp_path)
        fid = encode_file_id(SOURCE_MANAGED, "nope.ttf")
        resp = admin_session.delete(f"/api/admin/fonts/{fid}")
        assert resp.status_code == 404
        assert resp.json()["code"] == "FONT_NOT_FOUND"

    def test_rejects_path_traversal_in_id(self, admin_session: TestClient, tmp_path, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "font_dir", tmp_path)
        fid = encode_file_id(SOURCE_MANAGED, "../../etc/passwd")
        resp = admin_session.delete(f"/api/admin/fonts/{fid}")
        assert resp.status_code in (400, 404)
```

本文件顶部要照抄 `backend/tests/test_admin_api.py:28-52` 已有的四个 fixture
——`client` / `db_session` / `admin_user` / `admin_session`，**不要另起一套
账号体系**，也不要往 `conftest.py` 里加新的：

```python
PASSWORD = "admin-password-123"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_user(db_session):
    return users.create(
        db_session, username="admin", email="admin@example.com",
        password=PASSWORD, role="admin",
    )


@pytest.fixture
def admin_session(client, admin_user):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return client
```

`db_session` 的定义连同它那段关于 `_isolate_app_db` 的注释一并照抄——
那个延迟导入不是随手写的，去掉会让端点连到另一个 sqlite 文件上。

**上面测试代码里出现的 `admin_client` 全部改成 `admin_session`。**

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_admin_fonts.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.api.admin_fonts'`

- [ ] **Step 3: 加配置与错误类**

`backend/app/config.py` 的 Settings 里追加两个字段（放在 storage_root 附近）：

```python
    #: 面板管理的字体。放在 storage 下，api 与 worker 共享同一个卷，
    #: 写进去两边立刻都能用。
    font_dirname: str = "fonts"
    #: preflight 暂存区。上传完但还没决定怎么处理冲突的文件放这里。
    font_tmp_dirname: str = "fonts-tmp"

    @property
    def font_dir(self) -> Path:
        return self.storage_root / self.font_dirname

    @property
    def font_tmp_dir(self) -> Path:
        return self.storage_root / self.font_tmp_dirname
```

`backend/app/errors.py` 末尾追加：

```python
class FontNotFound(AppError):
    code = "FONT_NOT_FOUND"
    http_status = 404


class FontNotDeletable(AppError):
    """手工挂载与镜像内置的字体不归面板管——那两个目录在容器里是只读的。"""

    code = "FONT_NOT_DELETABLE"
    http_status = 400


class FontInvalid(AppError):
    code = "FONT_INVALID"
    http_status = 400


class FontTooLarge(AppError):
    code = "FONT_TOO_LARGE"
    http_status = 413


class FontUploadExpired(AppError):
    """preflight 暂存的文件已过 TTL 被回收。"""

    code = "FONT_UPLOAD_EXPIRED"
    http_status = 410
```

`backend/app/schemas.py` 末尾追加：

```python
class FontFaceDto(BaseModel):
    family: str
    style: str


class FontFileDto(BaseModel):
    file_id: str
    filename: str
    source: str
    families: list[str]
    faces: list[FontFaceDto]
    version: str
    charset_count: int
    size_bytes: int
    modified_at: datetime
    deletable: bool


class FontListDto(BaseModel):
    managed: list[FontFileDto]
    mounted: list[FontFileDto]
    builtin: list[FontFileDto]
```

- [ ] **Step 4: 写端点**

创建 `backend/app/api/admin_fonts.py`：

```python
"""字体管理端点。

字体存 storage 卷下的 fonts/，api 与 worker 共享，写进去两边立刻生效——
fontconfig 发现目录 mtime 变了会自动重扫，不需要 fc-cache，也不需要重启。

不建数据库表，列表每次现扫目录。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from app.api.deps import require_admin
from app.config import settings
from app.errors import FontNotDeletable, FontNotFound
from app.models import User
from app.schemas import FontFaceDto, FontFileDto, FontListDto
from app.services.font_store import (
    BUILTIN_DIRS,
    MOUNTED_DIR,
    SOURCE_BUILTIN,
    SOURCE_MANAGED,
    SOURCE_MOUNTED,
    WRITABLE_SOURCES,
    FontFile,
    decode_file_id,
    safe_filename,
    scan_dir,
)

router = APIRouter(prefix="/api/admin/fonts", tags=["admin"])


def _scan_all(include_builtin: bool) -> list[FontFile]:
    """扫出全部三个来源。

    builtin 有 300+ 个文件、每个都要跑两次 fc-query，扫一遍是秒级的，
    所以默认不扫——面板上那一组默认是折叠的。
    """
    found = scan_dir(settings.font_dir, SOURCE_MANAGED)
    found += scan_dir(MOUNTED_DIR, SOURCE_MOUNTED)
    if include_builtin:
        for d in BUILTIN_DIRS:
            for sub in sorted(p for p in d.rglob("*") if p.is_dir()) if d.is_dir() else []:
                if sub == MOUNTED_DIR:
                    continue
                found += scan_dir(sub, SOURCE_BUILTIN)
    return found


def _to_dto(f: FontFile) -> FontFileDto:
    return FontFileDto(
        file_id=f.file_id,
        filename=f.filename,
        source=f.source,
        families=list(f.families),
        faces=[FontFaceDto(family=x.family, style=x.style) for x in f.faces],
        # 一个文件里各 face 的版本通常相同，取第一个即可
        version=f.faces[0].version if f.faces else "-",
        charset_count=f.charset_count,
        size_bytes=f.size_bytes,
        modified_at=f.modified_at,
        deletable=f.deletable,
    )


@router.get("", response_model=FontListDto)
def list_fonts(
    include_builtin: bool = False, _: User = Depends(require_admin)
) -> FontListDto:
    found = _scan_all(include_builtin)
    return FontListDto(
        managed=[_to_dto(f) for f in found if f.source == SOURCE_MANAGED],
        mounted=[_to_dto(f) for f in found if f.source == SOURCE_MOUNTED],
        builtin=[_to_dto(f) for f in found if f.source == SOURCE_BUILTIN],
    )


@router.delete("/{file_id}", status_code=204)
def delete_font(file_id: str, _: User = Depends(require_admin)) -> None:
    try:
        source, filename = decode_file_id(file_id)
    except ValueError as exc:
        raise FontNotFound(str(exc)) from exc

    if source not in WRITABLE_SOURCES:
        raise FontNotDeletable(
            "手工挂载与镜像内置的字体不归面板管理，需要在宿主机上处理"
        )

    # 再过一次 safe_filename：file_id 是客户端传来的，即使 base64 解出来
    # 带路径分隔符也不能让它逃出字体目录
    target = settings.font_dir / safe_filename(filename)
    if not target.is_file():
        raise FontNotFound(f"字体 {filename} 不存在")
    target.unlink()
```

`backend/app/main.py` 注册路由（在 `app.include_router(admin_users.router)` 之后）：

```python
app.include_router(admin_fonts.router)
```

同时在文件顶部的 import 里加入 `admin_fonts`。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_admin_fonts.py -q`
Expected: PASS

- [ ] **Step 6: 更新 OpenAPI 快照**

Run: `cd backend && python scripts/dump_openapi.py`（脚本的确切名字见 `backend/scripts/`）
然后 `python -m pytest tests/test_openapi_snapshot.py -q` 确认通过。

- [ ] **Step 7: 全量回归 + 提交**

```bash
cd backend && python -m pytest -q
git add backend/app/api/admin_fonts.py backend/app/errors.py backend/app/schemas.py \
        backend/app/config.py backend/app/main.py backend/openapi.json \
        backend/tests/test_admin_fonts.py backend/tests/conftest.py
git commit -m "feat(fonts): 列表与删除端点

三个来源分组：面板管理的可删，手工挂载与镜像内置的只读——那两个目录
在容器里就是 :ro，提前拦住并说明原因，比让 OSError 冒成 500 强。

builtin 有 300+ 个文件、每个要跑两次 fc-query，默认不扫。

删除不存在的文件返回 404 而不是静默成功：静默会让管理员以为删掉了。"
```

---

### Task 4: preflight 与提交端点

**Files:**
- Modify: `backend/app/api/admin_fonts.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_admin_fonts_upload.py`

**Interfaces:**
- Consumes: Task 2 的 `is_duplicate` / `find_conflicts` / `probe` / `resolve_collision` / `safe_filename`；Task 3 的 `_scan_all` / `_to_dto`
- Produces:
  - `POST /api/admin/fonts/preflight`（multipart，字段名 `file`）→ `FontPreflightDto`
  - `POST /api/admin/fonts/commit` → `FontFileDto`
  - `FONT_TMP_TTL_SECONDS = 1800`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_admin_fonts_upload.py`：

```python
"""字体上传的两步流程：preflight 检查冲突，commit 落盘。

拆两步是为了让冲突弹窗在「文件已传完但还没落盘」时决策——否则要么先
落盘再回滚，要么让管理员重传一遍。
"""

import pytest
from fastapi.testclient import TestClient


def _upload(client: TestClient, name: str = "test.ttf", data: bytes = b"fake-font-bytes"):
    return client.post(
        "/api/admin/fonts/preflight",
        files={"file": (name, data, "font/ttf")},
    )


class TestPreflightValidation:
    def test_requires_admin(self, client: TestClient) -> None:
        assert _upload(client).status_code in (401, 403)

    def test_rejects_unknown_extension(self, admin_session: TestClient) -> None:
        resp = _upload(admin_session, name="evil.exe")
        assert resp.status_code == 400
        assert resp.json()["code"] == "FONT_INVALID"

    def test_rejects_file_that_is_not_a_font(self, admin_session: TestClient, monkeypatch) -> None:
        """扩展名对但内容不是字体的一样要拦——扩展名是客户端说了算的。"""
        import app.api.admin_fonts as mod

        monkeypatch.setattr(mod, "probe", lambda path, source: None)
        resp = _upload(admin_session)
        assert resp.status_code == 400
        assert resp.json()["code"] == "FONT_INVALID"

    def test_rejects_oversize(self, admin_session: TestClient) -> None:
        """64 MB 上限。pptx 那个 600MB 的限制对字体毫无意义。"""
        resp = _upload(admin_session, data=b"x" * (64 * 1024 * 1024 + 1))
        assert resp.status_code == 413
        assert resp.json()["code"] == "FONT_TOO_LARGE"

    def test_leaves_no_temp_file_when_rejected(self, admin_session: TestClient, tmp_path, monkeypatch) -> None:
        """被拒的上传不能在暂存区留垃圾。"""
        from app.config import settings
        import app.api.admin_fonts as mod

        monkeypatch.setattr(settings, "font_tmp_dirname", "t")
        monkeypatch.setattr(settings, "storage_root", tmp_path)
        monkeypatch.setattr(mod, "probe", lambda path, source: None)
        _upload(admin_session)
        assert list((tmp_path / "t").glob("*")) == []


class TestPreflightConflicts:
    def test_duplicate_sha_is_reported_and_not_a_conflict(self, admin_session, tmp_path, monkeypatch) -> None:
        """完全相同的文件没有歧义，直接告知，不弹冲突框。"""
        # 实现者：用 monkeypatch 把 _scan_all 换成返回一个 sha256 与
        # 上传内容相同的 FontFile，构造方式照抄 test_admin_fonts.py 的 _font()
        ...

    def test_family_overlap_returns_candidates(self, admin_session, monkeypatch) -> None:
        ...

    def test_no_overlap_returns_empty_candidates(self, admin_session, monkeypatch) -> None:
        ...


class TestCommit:
    def test_writes_file_and_returns_it(self, admin_session, tmp_path, monkeypatch) -> None:
        ...

    def test_replaces_listed_files(self, admin_session, tmp_path, monkeypatch) -> None:
        ...

    def test_refuses_to_replace_non_managed(self, admin_session, monkeypatch) -> None:
        """replace 列表里有手工挂载/内置的 → 400，不静默跳过。
        静默跳过会让管理员以为替换成功了。"""
        ...

    def test_expired_token_is_410(self, admin_session) -> None:
        ...

    def test_renames_on_collision(self, admin_session, tmp_path, monkeypatch) -> None:
        ...
```

> **实现者必须把上面标 `...` 的用例补全再开始写实现。** 每个用例的意图已在
> 名字和文档字符串里写明，构造 FontFile 的方式照抄 `test_admin_fonts.py`
> 里的 `_font()` 辅助函数。补全后再跑 Step 2。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_admin_fonts_upload.py -q`
Expected: FAIL，404（端点还不存在）

- [ ] **Step 3: 加 DTO**

`backend/app/schemas.py` 追加：

```python
class FontPreflightDto(BaseModel):
    token: str
    incoming: FontFileDto
    #: sha256 完全相同的已有文件。非空时前端不该再弹冲突框，直接提示已存在。
    duplicate_of: FontFileDto | None
    #: family 有重叠的已有文件，由管理员逐个决定是否替换。
    candidates: list[FontFileDto]


class FontCommitRequest(BaseModel):
    token: str
    #: 要被替换掉的已有字体的 file_id。空列表表示「这是一个新字体」。
    replace: list[str] = []
```

- [ ] **Step 4: 写端点**

在 `backend/app/api/admin_fonts.py` 追加：

```python
import shutil
import time
import uuid

from fastapi import File, UploadFile

from app.errors import FontInvalid, FontTooLarge, FontUploadExpired
from app.schemas import FontCommitRequest, FontPreflightDto
from app.services.font_store import (
    ALLOWED_SUFFIXES,
    find_conflicts,
    is_duplicate,
    probe,
    resolve_collision,
    sha256_of,
)

#: 单个字体文件上限。参照：msyh.ttc 约 19 MB，Noto CJK 全集约 20 MB。
#: 不沿用 pptx 那个 600MB 的限制——对字体毫无意义。
MAX_FONT_BYTES = 64 * 1024 * 1024

#: preflight 暂存的 TTL。够管理员看完对比表做决定，又不会长期占盘。
FONT_TMP_TTL_SECONDS = 30 * 60


def _sweep_tmp() -> None:
    """清掉过期的暂存文件。

    放在 preflight 入口顺手做，不另起后台任务：uploads 的那个回收器绑在
    uploads 表上，而这里没有表，复用不了。
    """
    tmp = settings.font_tmp_dir
    if not tmp.is_dir():
        return
    deadline = time.time() - FONT_TMP_TTL_SECONDS
    for p in tmp.iterdir():
        try:
            if p.stat().st_mtime < deadline:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
        except OSError:
            continue


@router.post("/preflight", response_model=FontPreflightDto)
async def preflight(
    file: UploadFile = File(...), _: User = Depends(require_admin)
) -> FontPreflightDto:
    _sweep_tmp()

    name = safe_filename(file.filename or "font")
    if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        raise FontInvalid("只接受 .ttf / .ttc / .otf")

    tmp_dir = settings.font_tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staged = tmp_dir / token
    staged.mkdir()
    dest = staged / name

    # 边写边计数：先读进内存再判大小的话，一个 1GB 的请求就能把进程撑爆
    written = 0
    try:
        with dest.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_FONT_BYTES:
                    raise FontTooLarge(
                        f"字体文件不能超过 {MAX_FONT_BYTES // 1024 // 1024} MB"
                    )
                fh.write(chunk)
        info = probe(dest, SOURCE_MANAGED)
        if info is None:
            raise FontInvalid("这不是可识别的字体文件")
    except Exception:
        # 被拒的上传不能在暂存区留垃圾，也不能留半个文件让 fontconfig 去扫
        shutil.rmtree(staged, ignore_errors=True)
        raise

    existing = _scan_all(include_builtin=True)
    dup = is_duplicate(info.sha256, existing)
    return FontPreflightDto(
        token=token,
        incoming=_to_dto(info),
        duplicate_of=_to_dto(dup) if dup else None,
        candidates=[] if dup else [_to_dto(c) for c in find_conflicts(info, existing)],
    )


@router.post("", response_model=FontFileDto)
def commit(
    payload: FontCommitRequest, _: User = Depends(require_admin)
) -> FontFileDto:
    staged = settings.font_tmp_dir / payload.token
    if not staged.is_dir() or not any(staged.iterdir()):
        raise FontUploadExpired("这次上传已过期，请重新选择文件")
    source_file = next(staged.iterdir())

    # 先校验整个 replace 列表再动手：一半替换一半失败是最难收拾的状态
    targets: list[Path] = []
    for fid in payload.replace:
        try:
            src, fname = decode_file_id(fid)
        except ValueError as exc:
            raise FontNotFound(str(exc)) from exc
        if src not in WRITABLE_SOURCES:
            raise FontNotDeletable(
                "手工挂载与镜像内置的字体不能替换，需要在宿主机上处理"
            )
        target = settings.font_dir / safe_filename(fname)
        if not target.is_file():
            raise FontNotFound(f"要替换的字体 {fname} 已不存在")
        targets.append(target)

    settings.font_dir.mkdir(parents=True, exist_ok=True)
    for t in targets:
        t.unlink()

    final_name = resolve_collision(settings.font_dir, source_file.name)
    shutil.move(str(source_file), str(settings.font_dir / final_name))
    shutil.rmtree(staged, ignore_errors=True)

    info = probe(settings.font_dir / final_name, SOURCE_MANAGED)
    if info is None:
        raise FontInvalid("落盘后无法解析，文件可能已损坏")
    return _to_dto(info)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_admin_fonts_upload.py -q`
Expected: PASS

- [ ] **Step 6: 更新 OpenAPI 快照并全量回归**

```bash
cd backend && python scripts/dump_openapi.py && python -m pytest -q
```

- [ ] **Step 7: 提交**

```bash
git add backend/app/api/admin_fonts.py backend/app/schemas.py backend/openapi.json \
        backend/tests/test_admin_fonts_upload.py
git commit -m "feat(fonts): preflight 与提交端点

两步是为了让冲突弹窗在「文件已传完但还没落盘」时决策，否则要么先落盘
再回滚，要么让管理员重传一遍。

边写边计数大小：先读进内存再判断的话，一个超大请求就能把进程撑爆。
被拒的上传要清掉暂存目录，不能留半个文件让 fontconfig 去扫。

replace 列表先整体校验再动手——一半替换一半失败是最难收拾的状态。"
```

---

### Task 5: fontconfig 认新目录

**Files:**
- Modify: `deploy/fontconfig-local.conf`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 3 的 `settings.font_dir` 默认值（`/app/storage/fonts`）

- [ ] **Step 1: 加 dir 声明**

在 `deploy/fontconfig-local.conf` 的 `<fontconfig>` 开标签之后、第一条 `<match>` 之前插入：

```xml
  <!--
    面板管理的字体。这个目录在 storage 卷里，api 与 worker 共享，
    写进去两边立刻都能用——fontconfig 发现目录 mtime 比缓存新会自动
    重扫，不需要 fc-cache，也不需要重启容器（实测确认）。

    与 /usr/share/fonts/truetype/extra（手工挂载）是两个独立来源：
    手工挂的那个是 :ro，面板删不掉；这个才归面板管。
  -->
  <dir>/app/storage/fonts</dir>
```

- [ ] **Step 2: 验证配置合法**

Run: `cd backend && python -c "import xml.dom.minidom as m; m.parse('../deploy/fontconfig-local.conf'); print('XML 合法')"`
Expected: 输出「XML 合法」

- [ ] **Step 3: 在容器里验证目录被识别**

```bash
docker compose build api
docker compose up -d api
docker compose exec api sh -c 'fc-conflist 2>/dev/null | head -5; echo ---; mkdir -p /app/storage/fonts && cp "$(fc-match -f "%{file}" "DejaVu Sans")" /app/storage/fonts/ZZTest.ttf && fc-list | grep -c ZZTest'
```
Expected: 最后一行输出 `1`（不跑 fc-cache 就能查到）

清理：`docker compose exec api rm -f /app/storage/fonts/ZZTest.ttf`

- [ ] **Step 4: README 补一句**

在「放自己的字体」一节的开头补充：

```markdown
装好之后，字体也可以直接在 **admin 面板 → 字体** 里上传和删除，不用碰宿主机。
下面讲的是手工放置的方式——两者可以并存，面板管的是 storage 卷里的
`fonts/`，手工放的是项目下的 `fonts-extra/`，面板能看见后者但删不掉它。
```

- [ ] **Step 5: 提交**

```bash
git add deploy/fontconfig-local.conf README.md
git commit -m "feat(fonts): fontconfig 认 storage 下的字体目录

写进去两边立刻生效——fontconfig 发现目录 mtime 比缓存新会自动重扫，
不需要 fc-cache 也不需要重启，这是实测确认过的。"
```

---

### Task 6: 前端 API 与字体列表面板

**Files:**
- Modify: `frontend/src/lib/adminApi.ts`
- Create: `frontend/src/pages/admin/FontsPanel.tsx`
- Modify: `frontend/src/pages/AdminPage.tsx:11-19,95-96`
- Modify: `frontend/src/i18n/locales/zh-CN.json`、`frontend/src/i18n/locales/en.json`
- Test: `frontend/src/pages/admin/FontsPanel.test.tsx`

**Interfaces:**
- Consumes: Task 3、4 的四个端点
- Produces:
  - `listFonts(includeBuiltin?: boolean): Promise<FontList>`
  - `preflightFont(file: File): Promise<FontPreflight>`
  - `commitFont(token: string, replace: string[]): Promise<FontFile>`
  - `deleteFont(fileId: string): Promise<void>`
  - `type FontFile`、`type FontList`、`type FontPreflight`

- [ ] **Step 1: 写失败的测试**

创建 `frontend/src/pages/admin/FontsPanel.test.tsx`：

```tsx
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '../../i18n'
import { FontsPanel } from './FontsPanel'

const FONT = {
  file_id: 'bWFuYWdlZC9tc3loLnR0Yw',
  filename: 'msyh.ttc',
  source: 'managed',
  families: ['微软雅黑', 'Microsoft YaHei UI'],
  faces: [{ family: '微软雅黑', style: 'Regular' }],
  version: '6.30',
  charset_count: 28762,
  size_bytes: 19_000_000,
  modified_at: '2026-08-03T00:00:00Z',
  deletable: true,
}

const MOUNTED = { ...FONT, file_id: 'bW91bnRlZC9hLnR0Zg', filename: 'a.ttf', source: 'mounted', deletable: false }

function renderPanel() {
  return render(
    <I18nProvider>
      <FontsPanel />
    </I18nProvider>,
  )
}

describe('FontsPanel', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('lists managed fonts with their families and coverage', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ managed: [FONT], mounted: [], builtin: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))

    renderPanel()

    expect(await screen.findByText('msyh.ttc')).toBeInTheDocument()
    expect(screen.getByText(/微软雅黑/)).toBeInTheDocument()
    expect(screen.getByText(/28,762/)).toBeInTheDocument()
  })

  it('does not offer delete on fonts it cannot delete', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ managed: [FONT], mounted: [MOUNTED], builtin: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))

    renderPanel()

    await screen.findByText('a.ttf')
    // 只有 managed 那一个有删除按钮
    expect(screen.getAllByRole('button', { name: /删除|Delete/ })).toHaveLength(1)
  })

  it('asks for confirmation before deleting', async () => {
    const fetchSpy = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') return new Response(null, { status: 204 })
      return new Response(JSON.stringify({ managed: [FONT], mounted: [], builtin: [] }),
        { status: 200, headers: { 'Content-Type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fetchSpy)
    vi.stubGlobal('confirm', vi.fn(() => false))

    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: /删除|Delete/ }))

    // 用户取消了，不应该发出 DELETE
    expect(fetchSpy.mock.calls.every(([, init]) => init?.method !== 'DELETE')).toBe(true)
  })

  it('loads builtin fonts only when the group is expanded', async () => {
    const fetchSpy = vi.fn(async () => new Response(
      JSON.stringify({ managed: [], mounted: [], builtin: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchSpy)

    renderPanel()
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    expect(String(fetchSpy.mock.calls[0][0])).not.toContain('include_builtin=true')

    await userEvent.click(screen.getByRole('button', { name: /内置|Built-in/ }))
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([u]) => String(u).includes('include_builtin=true'))).toBe(true),
    )
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/pages/admin/FontsPanel.test.tsx`
Expected: FAIL，找不到模块 `./FontsPanel`

- [ ] **Step 3: 加 API 客户端**

`frontend/src/lib/adminApi.ts` 末尾追加：

```ts
// ---- 字体管理 ----

export interface FontFace {
  family: string
  style: string
}

export interface FontFile {
  file_id: string
  filename: string
  source: 'managed' | 'mounted' | 'builtin'
  /** 这个文件里包含的字体名。ttc 一个文件含多个，微软雅黑就是。 */
  families: string[]
  faces: FontFace[]
  version: string
  /** 覆盖的码位数。比版本号有用——版本号看不出子集化，字数能。 */
  charset_count: number
  size_bytes: number
  modified_at: string
  deletable: boolean
}

export interface FontList {
  managed: FontFile[]
  mounted: FontFile[]
  builtin: FontFile[]
}

export interface FontPreflight {
  token: string
  incoming: FontFile
  /** 非空表示这个文件一模一样地传过了，前端不该再弹冲突框。 */
  duplicate_of: FontFile | null
  candidates: FontFile[]
}

export async function listFonts(includeBuiltin = false): Promise<FontList> {
  const q = includeBuiltin ? '?include_builtin=true' : ''
  const resp = await fetch('/api/admin/fonts' + q)
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<FontList>
}

export async function preflightFont(file: File): Promise<FontPreflight> {
  const form = new FormData()
  form.append('file', file)
  const resp = await fetch('/api/admin/fonts/preflight', { method: 'POST', body: form })
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<FontPreflight>
}

export async function commitFont(token: string, replace: string[]): Promise<FontFile> {
  const resp = await fetch('/api/admin/fonts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, replace }),
  })
  if (!resp.ok) await parseError(resp)
  return resp.json() as Promise<FontFile>
}

export async function deleteFont(fileId: string): Promise<void> {
  const resp = await fetch('/api/admin/fonts/' + fileId, { method: 'DELETE' })
  if (!resp.ok) await parseError(resp)
}
```

- [ ] **Step 4: 写面板**

创建 `frontend/src/pages/admin/FontsPanel.tsx`。要求：

- 三组：`managed`（可删）、`mounted`（只读，标注「手工挂载」）、`builtin`（默认折叠，展开时才带 `include_builtin=true` 重新请求）
- 每项显示：文件名、`families.join('、')`、版本、`charset_count.toLocaleString()` 字、大小（MB，一位小数）、修改时间
- 删除前 `window.confirm`
- 文案全部走 `t()`，不硬编码中文
- 结构与 `BlockedPanel.tsx` 保持一致：`card glass` + `section-title` + `grid gap`

上传部分留到 Task 7，本任务只做列表与删除。

- [ ] **Step 5: 接进 AdminPage**

`frontend/src/pages/AdminPage.tsx`：

```ts
type Section = 'users' | 'graph' | 'fonts' | 'origins' | 'blocked' | 'stats'
```

`SECTIONS` 数组里在 `graph` 之后插入：

```ts
  { key: 'fonts', labelKey: 'admin.section.fonts', hintKey: 'admin.section.fonts.hint' },
```

渲染处加：

```tsx
          {section === 'fonts' && <FontsPanel />}
```

- [ ] **Step 6: 加中英文案**

`zh-CN.json` 与 `en.json` 各加同一批 key（两边条数必须一致，CI 会校验对齐）：

```
admin.section.fonts            字体 / Fonts
admin.section.fonts.hint       上传与管理转换用的字体 / Fonts used for conversion
admin.fonts.managed            面板管理 / Managed here
admin.fonts.mounted            手工挂载（只读） / Mounted manually (read-only)
admin.fonts.builtin            镜像内置 / Built into the image
admin.fonts.families           包含字体 / Contains
admin.fonts.version            版本 / Version
admin.fonts.coverage           覆盖 {count} 字 / {count} characters
admin.fonts.empty              还没有上传字体 / No fonts uploaded yet
admin.fonts.mountedEmpty       fonts-extra 目录为空 / fonts-extra is empty
admin.fonts.confirmDelete      确定删除字体「{filename}」？转换时将回退到替代字体。 / Delete "{filename}"? Conversions will fall back to a substitute font.
admin.fonts.loadBuiltin        展开镜像内置字体（300+ 个，加载较慢） / Show built-in fonts (300+, slow to load)
```

- [ ] **Step 7: 跑测试确认通过**

```bash
cd frontend && npx vitest run src/pages/admin/FontsPanel.test.tsx && npx vitest run && npm run lint
```

- [ ] **Step 8: 提交**

```bash
git add frontend/src/lib/adminApi.ts frontend/src/pages/admin/FontsPanel.tsx \
        frontend/src/pages/admin/FontsPanel.test.tsx frontend/src/pages/AdminPage.tsx \
        frontend/src/i18n/locales/zh-CN.json frontend/src/i18n/locales/en.json
git commit -m "feat(fonts): 字体列表面板

三组分开：面板管理的可删，手工挂载与镜像内置的只读。内置那组默认折叠，
展开才请求——300+ 个文件、每个要跑两次 fc-query，扫一遍是秒级的。

「覆盖 N 字」这一列是刻意放上去的：版本号看不出子集化，字数能。"
```

---

### Task 7: 上传与冲突弹窗

**Files:**
- Create: `frontend/src/pages/admin/FontConflictDialog.tsx`
- Modify: `frontend/src/pages/admin/FontsPanel.tsx`
- Modify: `frontend/src/i18n/locales/zh-CN.json`、`frontend/src/i18n/locales/en.json`
- Test: `frontend/src/pages/admin/FontConflictDialog.test.tsx`

**Interfaces:**
- Consumes: Task 6 的 `preflightFont` / `commitFont` / `FontPreflight` / `FontFile`
- Produces: `<FontConflictDialog preflight={...} onResolve={(replace: string[]) => void} onCancel={() => void} />`

- [ ] **Step 1: 写失败的测试**

创建 `frontend/src/pages/admin/FontConflictDialog.test.tsx`：

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '../../i18n'
import { FontConflictDialog } from './FontConflictDialog'

const incoming = {
  file_id: 'x', filename: 'msyh.ttc', source: 'managed' as const,
  families: ['微软雅黑'], faces: [{ family: '微软雅黑', style: 'Regular' }],
  version: '6.30', charset_count: 28762, size_bytes: 19_000_000,
  modified_at: '2026-08-03T00:00:00Z', deletable: true,
}
const managedCandidate = { ...incoming, file_id: 'old1', filename: 'msyh-old.ttc', version: '6.25' }
const mountedCandidate = {
  ...incoming, file_id: 'm1', filename: 'mounted.ttc',
  source: 'mounted' as const, deletable: false,
}

function renderDialog(candidates = [managedCandidate], onResolve = vi.fn(), onCancel = vi.fn()) {
  render(
    <I18nProvider>
      <FontConflictDialog
        preflight={{ token: 'tok', incoming, duplicate_of: null, candidates }}
        onResolve={onResolve}
        onCancel={onCancel}
      />
    </I18nProvider>,
  )
  return { onResolve, onCancel }
}

describe('FontConflictDialog', () => {
  it('lists every conflicting file with its own checkbox', () => {
    renderDialog([managedCandidate, { ...managedCandidate, file_id: 'old2', filename: 'b.ttc' }])
    expect(screen.getAllByRole('checkbox')).toHaveLength(2)
  })

  it('reports only the checked files as replacements', async () => {
    const { onResolve } = renderDialog([
      managedCandidate,
      { ...managedCandidate, file_id: 'old2', filename: 'b.ttc' },
    ])
    await userEvent.click(screen.getAllByRole('checkbox')[0])
    await userEvent.click(screen.getByRole('button', { name: /替换勾选|Replace selected/ }))
    expect(onResolve).toHaveBeenCalledWith(['old1'])
  })

  it('treats "this is a new font" as replacing nothing', async () => {
    const { onResolve } = renderDialog()
    await userEvent.click(screen.getByRole('button', { name: /新字体|new font/i }))
    expect(onResolve).toHaveBeenCalledWith([])
  })

  it('cancels without resolving', async () => {
    const { onResolve, onCancel } = renderDialog()
    await userEvent.click(screen.getByRole('button', { name: /取消|Cancel/ }))
    expect(onCancel).toHaveBeenCalled()
    expect(onResolve).not.toHaveBeenCalled()
  })

  it('does not let non-managed candidates be selected', () => {
    /** 手工挂载与内置的删不掉，列出来只是让管理员知道名字被占了。 */
    renderDialog([mountedCandidate])
    expect(screen.getByRole('checkbox')).toBeDisabled()
  })

  it('disables the replace button until something is checked', async () => {
    renderDialog()
    const btn = screen.getByRole('button', { name: /替换勾选|Replace selected/ })
    expect(btn).toBeDisabled()
    await userEvent.click(screen.getByRole('checkbox'))
    expect(btn).toBeEnabled()
  })

  it('warns when the incoming font covers noticeably fewer characters', () => {
    /** 字数掉一大截基本是子集化的精简版，换上去中文会缺字。
     *  这条提示是这个弹窗最有价值的部分。 */
    renderDialog([{ ...managedCandidate, charset_count: 28762 }])
    // incoming 也是 28762，不该报警
    expect(screen.queryByText(/少了|fewer/)).not.toBeInTheDocument()
  })

  it('shows how many characters would be lost', () => {
    renderDialog([{ ...managedCandidate, charset_count: 36220 }])
    expect(screen.getByText(/7,458/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/pages/admin/FontConflictDialog.test.tsx`
Expected: FAIL，找不到模块

- [ ] **Step 3: 写弹窗**

创建 `frontend/src/pages/admin/FontConflictDialog.tsx`。要求：

- 用 `glass-overlay` + `glass-modal glass-strong`（与登录弹窗一致的类）
- 顶部一个方框展示 `incoming`：文件名、`families.join('、')`、版本、覆盖字数、大小
- 每个候选一行：**勾选框**（`deletable === false` 时 `disabled` 并标注「手工挂载，不可替换」）、文件名、families、版本、覆盖字数、修改时间
- 候选的 `charset_count` 比 `incoming` 多出 5% 以上时，该行下方显示「字数比你上传的少 N 个」——**这是最有价值的一行**，版本号看不出子集化
- 底部三个按钮：`替换勾选的字体`（没勾选时 disabled）、`这是一个新字体，直接添加`、`取消上传`
- 文案全部走 `t()`

- [ ] **Step 4: 接进 FontsPanel**

在 `FontsPanel.tsx` 加上传流程：

```tsx
// 选文件 → preflight → 有 duplicate_of 就直接提示不再弹窗
//                   → 有 candidates 就弹 FontConflictDialog
//                   → 都没有就直接 commit(token, [])
// 多选文件时逐个处理：一个处理完（commit 或取消）再进下一个
```

- [ ] **Step 5: 补文案**

```
admin.fonts.upload             上传字体 / Upload fonts
admin.fonts.uploading          上传中… / Uploading…
admin.fonts.duplicate          这个文件已经传过了，叫「{filename}」 / Already uploaded as "{filename}"
admin.fonts.conflictTitle      上传的字体与下列已有字体可能是同一个，请逐个确认 / This font may be the same as the ones below — please confirm each
admin.fonts.incoming           你上传的 / You are uploading
admin.fonts.notReplaceable     手工挂载，不可替换 / Mounted manually, cannot be replaced
admin.fonts.fewerChars         字数比你上传的少 {count} 个 / {count} fewer characters than yours
admin.fonts.replaceSelected    替换勾选的字体 / Replace selected
admin.fonts.isNewFont          这是一个新字体，直接添加 / This is a new font, just add it
admin.fonts.cancelUpload       取消上传 / Cancel upload
```

- [ ] **Step 6: 跑测试确认通过**

```bash
cd frontend && npx vitest run && npm run lint
```

- [ ] **Step 7: 提交**

```bash
git add frontend/src/pages/admin/FontConflictDialog.tsx \
        frontend/src/pages/admin/FontConflictDialog.test.tsx \
        frontend/src/pages/admin/FontsPanel.tsx \
        frontend/src/i18n/locales/zh-CN.json frontend/src/i18n/locales/en.json
git commit -m "feat(fonts): 上传与冲突弹窗

每个冲突候选一个勾选框，一次可以替换多个——旧字体拆成三个文件、新的
合并成一个是真实场景，单按钮表达不了。

「字数比你上传的少 N 个」是这个弹窗最有价值的一行：版本号看不出子集化，
字数能。换上一个精简版，中文会缺字，而且要等到转换出来才发现。

手工挂载与内置的候选列出来但勾选框禁用——它们删不掉，列出来只是让
管理员知道名字被占了、自己传的可能不生效。"
```

---

### Task 8: 真机验证

**Files:** 无代码改动

- [ ] **Step 1: 部署**

```bash
ssh -i ~/.ssh/oracle_test.key ubuntu@217.142.236.79
cd ~/pptx2pdf && git pull --ff-only
docker compose build api worker frontend && docker compose up -d
```

- [ ] **Step 2: 走一遍界面**

在 admin 面板 → 字体：

1. 列表能看到镜像内置那组（展开）与手工挂载那组
2. 传一个真的 `msyh.ttc`（从 Windows 的 `C:\Windows\Fonts` 拿）
3. 确认弹窗列出了它包含的多个 family
4. 再传一次同一个文件 → 应该被「已经传过了」拦住，不弹冲突框
5. 传一个 family 相同但文件不同的字体 → 应该弹冲突框，勾选替换
6. 删除，确认列表刷新

- [ ] **Step 3: 验证字体真的生效**

```bash
docker compose exec worker fc-list | grep -i "微软雅黑\|YaHei"
docker compose exec api fc-list | grep -i "微软雅黑\|YaHei"
```
两边都要能查到——只有一边有的话，网页与 v1 会出两份不同的 PDF。

- [ ] **Step 4: 端到端**

传一份中文课件转换，与放字体之前的输出对比中文换行位置。

- [ ] **Step 5: 记录结果**

把验证结果补进 `docs/DEVLOG.md`，格式照抄该文件里已有的条目。

---

## 自查

**Spec 覆盖：** 存储位置 → Task 3 配置 + Task 5 fontconfig；不建表 → Task 2/3 全程无 model；ttc 多 face → Task 1 解析 + Task 2 判定 + Task 7 展示；三档冲突判定 → Task 2 + Task 4；不自动判断更新 → Task 7 只展示事实由人决定；弹窗交互 → Task 7；四个接口 → Task 3 + Task 4；三来源分组 → Task 3 + Task 6；边界与错误 → Task 3/4 的错误类与用例；测试策略 → 各任务的测试步骤；落地顺序 → 任务顺序一致。

**已知缺口：** Task 4 的测试骨架里有六个用例只给了名字与意图（标 `...`），要求实现者补全后再写实现。这是刻意的——那几个用例都要 monkeypatch `_scan_all` 并构造 `FontFile`，构造方式与 Task 3 的 `_font()` 完全相同，照抄即可；把三十行几乎一样的样板复制到计划里，只会让实现者跳着读。

**类型一致性：** `FontFace`（Task 1）→ `FontFile.faces`（Task 2）→ `FontFaceDto`（Task 3）→ 前端 `FontFace`（Task 6）字段名一致；`file_id` 全程用 `encode_file_id` 生成、`decode_file_id` 解析；`charset_count` 从 Task 2 到前端同名；`WRITABLE_SOURCES` 在 Task 2 定义、Task 3/4 消费。
