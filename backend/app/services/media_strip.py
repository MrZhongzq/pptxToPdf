"""剥离 pptx 里的内嵌视频与音频。

PDF 放不了视频——这些字节从进入系统的第一刻起就是纯浪费，却会让
一个本来能转的 deck 撞上分片上限（真机遇到过：83.7MB 的课件里第 25
页单页 56MB 视频，报 SHARD_TOO_LARGE 且单页无法再切分）。

复用 opc_rewrite 的通用工具，不另写一套：三期在切片上为同一类问题
修了五轮（悬空 Relationship、mc:Ignorable 被 ET 往返吃掉、正则手术
对空格/命名空间前缀/非自闭合三种变体的处理），那些教训不该重走。
"""

import os
import re
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

MEDIA_REL_TYPES: frozenset[str] = frozenset(
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


# PowerPoint 2010 媒体扩展的 uri，<p14:media> 就住在这个 <p:ext> 里。
_MEDIA_EXT_URI = "{DAA4B4D4-6D71-4841-9C94-3DE7FCFB9230}"


def _media_refs(zf: zipfile.ZipFile) -> tuple[set[str], dict[str, set[str]]]:
    """扫描全部 .rels，返回 (被媒体关系指向的 part, {owner part: 该 part 上的媒体 rId})。

    第二项是删正文引用用的：光删 part 和 Relationship 不够，见
    strip_media 的 docstring。
    """
    targets: set[str] = set()
    rids_by_owner: dict[str, set[str]] = {}
    for name in zf.namelist():
        if not name.endswith(".rels"):
            continue
        owner = owner_part(name)
        hits: set[str] = set()
        for rid, rel_type, target in read_rels(zf, owner):
            if rel_type in MEDIA_REL_TYPES:
                targets.add(target)
                hits.add(rid)
        if hits:
            rids_by_owner[owner] = hits
    return targets, rids_by_owner


def _clean_media_refs(xml: str, rids: set[str]) -> str:
    """删掉 slide 正文里指向已删媒体的引用。

    四处都得删，缺一处 Office 服务就整份拒绝（真机实测，见 strip_media
    的 docstring）。`<p:pic>` 与它的 `<p:blipFill>` 海报帧一律保留——
    摘掉媒体引用后它退化成一张普通图片，那一页显示的是视频封面而不是
    空白，这是信息损失最小的收法。
    """
    for rid in rids:
        q = re.escape(rid)
        # 1. <a:videoFile r:link="rIdX"/> / <a:audioFile .../>
        xml = re.sub(
            r'<a:(?:videoFile|audioFile)\b[^>]*\br:link="' + q + r'"[^>]*/>', "", xml
        )
        # 2. 包着 <p14:media r:embed="rIdX"/> 的整个 <p:ext>——p14:media 是
        #    2010 扩展，只留空 <p:ext> 也算多余节点，整块摘掉更干净。
        xml = re.sub(
            r"<p:ext\b[^>]*\buri=\"" + re.escape(_MEDIA_EXT_URI) + r"\"[^>]*>\s*"
            r'<p14:media\b[^>]*\br:embed="' + q + r'"[^>]*/>\s*</p:ext>',
            "",
            xml,
        )
    # 3. 「播放媒体」的点击动作——媒体没了，动作没有目标
    xml = re.sub(r'<a:hlinkClick\b[^>]*\baction="ppaction://media"[^>]*/>', "", xml)
    # 4. <p:timing> 里的 <p:video>/<p:audio>。它们用 <p:spTgt spid="N"/> 指
    #    形状、不带 r:id，所以删关系不会让它们「悬空」——但形状已经不再是
    #    媒体，Office 服务据此判定文档不一致。真机实测：只做 1-3 不做这条，
    #    仍然 406。
    xml = re.sub(r"<p:video>.*?</p:video>", "", xml, flags=re.S)
    xml = re.sub(r"<p:audio>.*?</p:audio>", "", xml, flags=re.S)
    # 上面摘空的容器不留残壳
    xml = re.sub(r"<p:extLst>\s*</p:extLst>", "", xml)
    return xml


def strip_media(src: Path) -> StripResult:
    """就地剥离内嵌媒体。不含媒体时不重写文件。

    删 part + Relationship + Content_Types 三样**不够**：slide 正文里
    还留着指向它们的引用，Office 在线服务会因此拒绝整份文档，返回
    HTTP 406 NotAcceptable（"Error from Office Service"）。真机实测，
    83.7MB 的课件剥掉两段视频后必然 406，直到把正文引用也清干净。

    这里曾经写着「悬空 r:id 同三期对内部跳转的裁决：消费方忽略非关键
    内容」——那个类比是错的，已被真实文件推翻。三期说的是超链接跳转，
    确属可忽略的装饰；而 <a:videoFile>/<p14:media> 是 <p:pic> 的结构性
    引用，<p:timing> 里的 <p:video> 更是直接声明「这个形状是媒体」。
    合成样本没暴露这一点，因为它只往 .rels 里注入关系、正文里压根没有
    引用那条关系的元素——测的是真实世界不存在的形状。

    清理的四处与验证过程见 _clean_media_refs。
    """
    bytes_before = src.stat().st_size

    with zipfile.ZipFile(src) as zf:
        drop, rids_by_owner = _media_refs(zf)
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
                elif name in rids_by_owner:
                    # 只有挂着媒体关系的 part 才做正文改写，其余仍走逐字节
                    # 复制——改写面越小，撞上 mc:Ignorable 那类坑的机会越小。
                    zout.writestr(
                        item,
                        _clean_media_refs(
                            zin.read(name).decode("utf-8"), rids_by_owner[name]
                        ).encode("utf-8"),
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
