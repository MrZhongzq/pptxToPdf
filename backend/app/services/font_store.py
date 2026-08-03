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
    CHARSET_FORMAT,
    FC_QUERY_FORMAT,
    FontFace,
    parse_charset,
    parse_fc_query,
)

SOURCE_MANAGED = "managed"
SOURCE_MOUNTED = "mounted"
SOURCE_BUILTIN = "builtin"

KNOWN_SOURCES = frozenset({SOURCE_MANAGED, SOURCE_MOUNTED, SOURCE_BUILTIN})

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
    # id 是客户端传来的，解出来的两截都要当成不可信输入。
    #
    # partition 只切第一个斜杠，剩下的原样留在 filename 里：构造
    # "managed//etc/passwd" 就能解出一个绝对路径，而 pathlib 的
    # `base / "/etc/passwd"` 会**整体替换**掉 base，变成任意文件读写。
    # 消费方那边也会再过一次 safe_filename，但把防线全押在「每个调用方
    # 都记得校验」上，漏一处就是任意删除，所以这里先堵死。
    if source not in KNOWN_SOURCES or filename != safe_filename(filename):
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
            ["fc-query", "--format", CHARSET_FORMAT, str(path)],
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
