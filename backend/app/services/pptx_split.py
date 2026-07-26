import logging
import posixpath
import re
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

PRESENTATION = "ppt/presentation.xml"
PKG_RELS = "_rels/.rels"

P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL_NS_URI = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS_URI = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "{" + REL_NS_URI + "}"
CT_NS = "{" + CT_NS_URI + "}"

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


def _owner_part(rels_name: str) -> str:
    """_rels_path 的反函数：
    ppt/slides/_rels/slide1.xml.rels -> ppt/slides/slide1.xml
    _rels/.rels -> ""（包级）。

    Relationship 的 Target 是相对于它的 owner part 所在目录解析的
    （见 _resolve），_rewrite_rels 需要知道 owner 才能正确判断某条
    Relationship 该不该被删——不能只有 presentation.xml.rels 用对了
    base_part，其余 .rels（slide 自己的、包级的）也一样需要。
    """
    d = posixpath.dirname(rels_name)
    base = posixpath.basename(rels_name)
    name = base[: -len(".rels")]
    parent_dir = posixpath.dirname(d)
    return posixpath.join(parent_dir, name) if name else parent_dir


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
    """从 part 出发递归收集依赖的所有 part（含它自己的 .rels）。

    不递归 SLIDE_REL_TYPE：保留的 slide 正文里可能有指向「另一页」的
    内部超链接（PowerPoint「链接到幻灯片」、动作按钮、目录跳转，
    生成的关系 <Relationship Type=".../slide" Target="slideN.xml"/>
    与 sldIdLst 里的 slide 关系用的是同一个 SLIDE_REL_TYPE）。如果不
    过滤，range 外的整页（连同它的 media）会顺着这条关系被拖进当前
    分片，还会级联到被拖入页自己的跳转目标。分片边界只能由 ranges
    决定，不能被 deck 内部的超链接改写。

    取舍：跳过之后，若跳转目标恰好不在这次分片的 keep_parts 里，
    _rewrite_rels 会把发起跳转的 slide 自己的 .rels 里那条
    Relationship 删掉，而它的正文里 r:id="rIdN" 还留着，变成悬空
    引用。接受这个代价——悬空 rId 只是让这一条超链接失效（PDF 转换
    环境会当成失效内容忽略），而不过滤的后果是分片的物理 slide part
    数量和 range 长度对不上、probe().slide_count 与后续引擎的输出
    页数校验必然失败。两害相权取其轻，宁可丢一条内部超链接。
    """
    if part in keep:
        return
    keep.add(part)
    rels_name = _rels_path(part)
    if rels_name in zf.namelist():
        keep.add(rels_name)
    for _rid, rel_type, target in _read_rels(zf, part):
        if rel_type in DROP_REL_TYPES:
            continue
        if rel_type == SLIDE_REL_TYPE:
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


# 定位 <p:sldId .../> 这类自闭合、无嵌套的简单元素：前缀不固定（约定
# 俗成是 "p"，但正则不假设），\b 保证 "sldId" 后面不会连着 "Lst" 之类
# 别的标签名（sldIdLst 是它的父元素，绝不能被这条正则误删）。
_SLD_ID_RE = re.compile(rb"<[A-Za-z0-9]*:?sldId\b[^>]*?/>")
_RID_ATTR_RE = re.compile(rb'r:id=["\']([^"\']+)["\']')


def _rewrite_presentation(raw: bytes, keep_rids: set[str]) -> bytes:
    """只删除 keep_rids 之外的 <p:sldId .../> 元素，其余字节原样不动。

    不用 ET 往返。真实 PowerPoint 产出的 presentation.xml 根元素常见
    形如 `<p:presentation ... xmlns:p14="..." mc:Ignorable="p14">`：
    presentation.xml 里通常没有任何 p14: 元素，ET.tostring 只序列化
    「实际被用到」的命名空间，于是 xmlns:p14 声明会被静默丢弃，而
    mc:Ignorable="p14" 却原样留下，变成指向一个作用域内未声明前缀的
    引用，违反 MCE（Markup Compatibility and Extensibility）规范。
    sldId 是自闭合、无嵌套的简单元素，用正则定位删除足够安全，且不
    触碰其余任何字节——根元素的全部 xmlns 声明、mc:Ignorable、
    standalone="yes" 都原样保留。
    """

    def _drop_if_unkept(m: "re.Match[bytes]") -> bytes:
        rid_m = _RID_ATTR_RE.search(m.group(0))
        if rid_m and rid_m.group(1).decode("ascii") in keep_rids:
            return m.group(0)
        return b""

    return _SLD_ID_RE.sub(_drop_if_unkept, raw)


def _rewrite_rels(raw: bytes, keep_parts: set[str], base_part: str) -> bytes:
    """删掉指向未保留 part 的 Relationship。

    rId 一律不重编号：保留的 slide XML 内部有 r:embed="rId3" 这类引用，
    重编号就要同步改写每个 slide 的正文，那是引入 bug 的捷径。

    .rels 文档结构扁平、不带 MCE 标记，可以放心用 ET 往返，但要在
    tostring 之前重新登记默认命名空间的前缀，否则 ET 会把
    `xmlns="...relationships"` 序列化成 `xmlns:ns0="...relationships"`
    这种合法但不是原始写法的形式。
    """
    ET.register_namespace("", REL_NS_URI)
    root = ET.fromstring(raw)
    for rel in list(root):
        if rel.get("TargetMode") == "External":
            continue
        target = _resolve(base_part, rel.get("Target"))
        if target not in keep_parts:
            root.remove(rel)
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8")


def _rewrite_content_types(raw: bytes, keep_parts: set[str]) -> bytes:
    """删掉未保留 part 的 Override。Default（按扩展名）全部保留。

    同 _rewrite_rels，序列化前重新登记默认命名空间前缀，理由同上。
    """
    ET.register_namespace("", CT_NS_URI)
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
                    elif name.endswith(".rels"):
                        # 不能只特判 pres_rels_name：DROP_REL_TYPES 排除
                        # 掉的 part（notesSlide / comments / thumbnail）
                        # 各自的宿主 part 仍然保留在这个分片里
                        # （slide 本身没被删，只是它指向 notesSlide 的
                        # 那条关系被删；docProps/thumbnail.jpeg 的宿主
                        # 是包级 _rels/.rels）。如果这里只重写
                        # presentation.xml.rels，其余所有 .rels
                        # （slide 自己的、slideLayout/slideMaster 的、
                        # 包级的）会被 else 分支逐字节原样搬过去，里面
                        # 指向已删 part 的 Relationship 一条不剩地留下，
                        # 变成 OPC 不允许的悬空引用。
                        zout.writestr(
                            name,
                            _rewrite_rels(
                                zin.read(name), keep_parts, _owner_part(name)
                            ),
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
