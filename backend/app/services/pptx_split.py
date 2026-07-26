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
            # presentation.xml 本身也要保留，但不能用 _collect(zin,
            # PRESENTATION, keep_parts)：_collect 会无差别递归它的全部
            # 关系，包括 SLIDE_REL_TYPE，把刚刚按范围裁剪掉的 slide 重新
            # 加回 keep_parts，range 裁剪形同虚设。这里只需要它自己这个
            # part（它的 .rels 已经在下面 keep_parts.update 里单独加了）。
            keep_parts.add(PRESENTATION)
            # 包级：docProps 等
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
