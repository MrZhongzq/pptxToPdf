"""剥离 pptx 里的内嵌视频与音频。

PDF 放不了视频——这些字节从进入系统的第一刻起就是纯浪费，却会让
一个本来能转的 deck 撞上分片上限（真机遇到过：83.7MB 的课件里第 25
页单页 56MB 视频，报 SHARD_TOO_LARGE 且单页无法再切分）。

复用 opc_rewrite 的通用工具，不另写一套：三期在切片上为同一类问题
修了五轮（悬空 Relationship、mc:Ignorable 被 ET 往返吃掉、正则手术
对空格/命名空间前缀/非自闭合三种变体的处理），那些教训不该重走。
"""

import os
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

    fd, tmp_name = tempfile.mkstemp(suffix=".pptx", dir=str(src.parent))
    os.close(fd)  # mkstemp 的 fd 不关掉，Windows 上 tmp.replace(src) 会因
    # 「文件仍被占用」报 PermissionError（POSIX 容忍，Windows 不容忍）。
    tmp = Path(tmp_name)
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
