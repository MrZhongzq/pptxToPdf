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
    return int(sld_sz.attrib["cx"]), int(sld_sz.attrib["cy"])


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
            slide_count = sum(1 for n in names if SLIDE_RE.match(n))
            if PRESENTATION_PART not in names:
                raise PptxNotPresentation("不是 PowerPoint 演示文稿")
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
