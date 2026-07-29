"""OPC（Open Packaging Conventions）包重写的通用工具。

这些函数处理的是「包里的关系怎么解析、怎么删」，与「按页范围裁剪」
无关——它们先住在 pptx_split.py 里只是历史顺序。媒体剥离需要同一套
机制，故提取共用：跨模块 import 私有函数是坏味道，而各写一套会让
第二份重新踩一遍三期在切片上修了五轮的那些坑（悬空 Relationship、
mc:Ignorable 被 ET 往返吃掉、rels 里 Target 相对路径的解析基准）。
"""

import posixpath
import zipfile
from xml.etree import ElementTree as ET

REL_NS_URI = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS_URI = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "{" + REL_NS_URI + "}"
CT_NS = "{" + CT_NS_URI + "}"


def rels_path(part: str) -> str:
    """ppt/slides/slide1.xml -> ppt/slides/_rels/slide1.xml.rels"""
    d, name = posixpath.split(part)
    return posixpath.join(d, "_rels", name + ".rels") if d else f"_rels/{name}.rels"


def owner_part(rels_name: str) -> str:
    """rels_path 的反函数：
    ppt/slides/_rels/slide1.xml.rels -> ppt/slides/slide1.xml
    _rels/.rels -> ""（包级）。

    Relationship 的 Target 是相对于它的 owner part 所在目录解析的
    （见 resolve），rewrite_rels 需要知道 owner 才能正确判断某条
    Relationship 该不该被删——不能只有 presentation.xml.rels 用对了
    base_part，其余 .rels（slide 自己的、包级的）也一样需要。
    """
    d = posixpath.dirname(rels_name)
    base = posixpath.basename(rels_name)
    name = base[: -len(".rels")]
    parent_dir = posixpath.dirname(d)
    return posixpath.join(parent_dir, name) if name else parent_dir


def resolve(base_part: str, target: str) -> str:
    """把 rels 里相对于 base_part 所在目录的 Target 解析成包内绝对路径。"""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def read_rels(zf: zipfile.ZipFile, part: str) -> list[tuple[str, str, str]]:
    """返回 [(rId, type, resolved_target)]，跳过外部链接。"""
    rels_name = rels_path(part)
    try:
        raw = zf.read(rels_name)
    except KeyError:
        return []
    out = []
    for rel in ET.fromstring(raw):
        if rel.get("TargetMode") == "External":
            continue
        out.append(
            (rel.get("Id"), rel.get("Type"), resolve(part, rel.get("Target")))
        )
    return out


def rewrite_rels(raw: bytes, keep_parts: set[str], base_part: str) -> bytes:
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
        target = resolve(base_part, rel.get("Target"))
        if target not in keep_parts:
            root.remove(rel)
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8")


def rewrite_content_types(raw: bytes, keep_parts: set[str]) -> bytes:
    """删掉未保留 part 的 Override。Default（按扩展名）全部保留。

    同 rewrite_rels，序列化前重新登记默认命名空间前缀，理由同上。
    """
    ET.register_namespace("", CT_NS_URI)
    root = ET.fromstring(raw)
    for node in list(root):
        if node.tag == f"{CT_NS}Override":
            part = node.get("PartName", "").lstrip("/")
            if part not in keep_parts:
                root.remove(node)
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8")
