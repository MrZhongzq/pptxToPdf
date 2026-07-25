import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from app.errors import PptxEncrypted, PptxInvalidZip, PptxNotPresentation

# OOXML 加密文件是 OLE 复合文档，不是 zip
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

PRESENTATION_PART = "ppt/presentation.xml"
SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")
FONT_SCAN_RE = re.compile(r"^ppt/(slides|slideMasters|slideLayouts|theme)/.+\.xml$")

# 隐藏标记在根元素 <p:sld ... show="0"> 的属性上，属性顺序不固定，
# 缺省即可见，只有显式 show="0" 才是隐藏。只在根元素的开标签内找，
# 避免误配子元素里恰好也叫 show 的属性。
SLIDE_HEAD_BYTES = 4096
HIDDEN_ATTR_RE = re.compile(rb'\bshow\s*=\s*"0"')

P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
FONT_TAGS = (f"{A_NS}latin", f"{A_NS}ea", f"{A_NS}cs")

MAX_FONT_PARTS = 200  # 超长 deck 只扫前若干个 part，字体清单不需要穷举


@dataclass(frozen=True)
class PptxMeta:
    slide_count: int
    slide_width_emu: int
    slide_height_emu: int
    fonts: tuple[str, ...]


def _is_encrypted(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(8) == CFB_MAGIC


def _read_slide_size(zf: zipfile.ZipFile) -> tuple[int, int]:
    try:
        raw = zf.read(PRESENTATION_PART)
    except KeyError as exc:
        raise PptxNotPresentation("缺少 ppt/presentation.xml") from exc

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise PptxNotPresentation(f"presentation.xml 解析失败: {exc}") from exc

    sld_sz = root.find(f"{P_NS}sldSz")
    if sld_sz is None:
        raise PptxNotPresentation("presentation.xml 缺少 sldSz")
    try:
        return int(sld_sz.attrib["cx"]), int(sld_sz.attrib["cy"])
    except (KeyError, ValueError) as exc:
        raise PptxNotPresentation(
            f"presentation.xml 的 sldSz 属性非法: cx={sld_sz.attrib.get('cx')!r}, "
            f"cy={sld_sz.attrib.get('cy')!r} ({exc})"
        ) from exc


def _is_slide_hidden(zf: zipfile.ZipFile, name: str) -> bool:
    """只读解压后的前 4KB 判断隐藏标记，不读整份 slide XML。

    根元素 <p:sld ...> 的开标签必然出现在文件最前面，500 页的 deck
    也不会让单页 XML 的开标签超过 4KB。
    """
    with zf.open(name) as fh:
        head = fh.read(SLIDE_HEAD_BYTES)
    end = head.find(b">")
    root_tag = head[: end + 1] if end != -1 else head
    return bool(HIDDEN_ATTR_RE.search(root_tag))


def _count_visible_slides(zf: zipfile.ZipFile, names: list[str]) -> int:
    """数可见页数——必须与 soffice 实际导出的页数口径一致。

    `--convert-to pdf:impress_pdf_Export` 的 ExportHiddenSlides 默认为
    false，隐藏页不会进 PDF。如果这里仍数文件数（含隐藏页），
    `_verify_output` 的页数校验永远不通过，一份完全正确的转换会被
    误判失败并删除。
    """
    visible = 0
    for name in names:
        try:
            hidden = _is_slide_hidden(zf, name)
        except (KeyError, OSError, zipfile.BadZipFile):
            # 单页读取失败时按可见计入，而不是跳过不计。理由：
            # 1) OOXML 里 show 属性缺省即可见，无法确认隐藏标记不等于
            #    有隐藏标记的证据；
            # 2) slide_count 是 _verify_output 页数校验的预期值，把它
            #    悄悄调小会让「实际漏导出一页」的真实故障被这次意外的
            #    读取失败掩盖过去——按可见计入至多导致误报一次页数不符
            #    （需要人工核查这一页），比放过真实缺页更安全。
            visible += 1
            continue
        if not hidden:
            visible += 1
    return visible


def _collect_fonts(zf: zipfile.ZipFile) -> tuple[str, ...]:
    fonts: set[str] = set()
    parts = [n for n in zf.namelist() if FONT_SCAN_RE.match(n)][:MAX_FONT_PARTS]
    for name in parts:
        try:
            root = ET.fromstring(zf.read(name))
        except (ET.ParseError, KeyError):
            continue  # 单个 part 坏掉不应让整次解析失败
        for tag in FONT_TAGS:
            for el in root.iter(tag):
                typeface = el.attrib.get("typeface", "").strip()
                # 跳过 +mj-lt / +mn-ea 这类主题占位引用
                if typeface and not typeface.startswith("+"):
                    fonts.add(typeface)
    return tuple(sorted(fonts))


def probe(path: Path) -> PptxMeta:
    """只读所需 zip 条目解析元信息，内存开销与文件大小无关。"""
    path = Path(path)
    if _is_encrypted(path):
        raise PptxEncrypted("文件已加密，无法解析")

    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            slide_names = [n for n in names if SLIDE_RE.match(n)]
            if PRESENTATION_PART not in names:
                raise PptxNotPresentation("不是 PowerPoint 演示文稿")
            slide_count = _count_visible_slides(zf, slide_names)
            width, height = _read_slide_size(zf)
            fonts = _collect_fonts(zf)
    except zipfile.BadZipFile as exc:
        raise PptxInvalidZip(f"不是合法的 zip 容器: {exc}") from exc

    return PptxMeta(
        slide_count=slide_count,
        slide_width_emu=width,
        slide_height_emu=height,
        fonts=fonts,
    )
