"""转换完成后对 PDF 做的两项变换：书签大纲与页边距重映射。

一期就把这两个选项写进了 ConversionOptions，后端一直只落库不实现，
前端标着「后端实现中」。六期补上。

第三个选项 expand_animations 不在这里——它必须在**转换前**改写 pptx
（把一页拆成多页），见 services/animation_expand.py。

两条转换路径都要接：单次转换在 pipeline.run_task，分片路径在
shard_pipeline.merge_shards 之后。书签必须**合并后**才加，否则每片的
页码基准不同。
"""

import logging
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject

from app.schemas import ConversionOptions

logger = logging.getLogger(__name__)

# 右侧留白占成品页宽的比例。用户要的是「给 iPad 竖屏批注留出侧边空白」，
# 没指定比例和方向。25% 是常见的批注栏宽度（横向 A4 约 7cm），右侧是
# 右手书写者的自然选择。做成常量而不是配置项：现在没有第二个用户提出
# 过不同需求，做成可配置属于 YAGNI。
MARGIN_RATIO = 0.25


def apply(pdf_path: Path, options: ConversionOptions, titles: list[str] | None = None) -> bool:
    """就地对 PDF 应用后处理。返回是否真的改写了文件。

    一项都没勾时直接返回 False、不碰文件——重写一遍 PDF 不是零成本
    （几十 MB 的课件要整份读进内存再吐出来），而且 pypdf 的往返会丢掉
    一些它不认识的对象，能不碰就不碰。
    """
    want_outline = options.pdf_outline and titles
    want_margins = options.remap_margins
    if not want_outline and not want_margins:
        return False

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()

    for page in reader.pages:
        if want_margins:
            _widen_page(page)
        writer.add_page(page)

    if want_outline:
        _add_outline(writer, titles)

    tmp = pdf_path.with_suffix(".pp.pdf")
    try:
        with tmp.open("wb") as fh:
            writer.write(fh)
        tmp.replace(pdf_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    logger.info(
        "postprocess applied path=%s outline=%s margins=%s pages=%d",
        pdf_path.name, bool(want_outline), bool(want_margins), len(reader.pages),
    )
    return True


def _widen_page(page) -> None:
    """把页面变宽，内容留在左侧，右边空出来供批注。

    做法是只改 MediaBox 的宽度而**不动内容**：页面坐标系原点在左下角，
    往右扩宽不会移动任何已有对象，矢量性、可选文字、内部链接全部原样
    保留。缩放并平移内容也能达到类似效果，但那会让正文变小、也更容易
    在有旋转的页面上出错。
    """
    box = page.mediabox
    width = float(box.right) - float(box.left)
    page.mediabox = RectangleObject(
        (
            float(box.left),
            float(box.bottom),
            float(box.right) + width * MARGIN_RATIO,
            float(box.top),
        )
    )
    # CropBox 若存在且比 MediaBox 小，阅读器按 CropBox 显示，扩宽会看不见。
    # 只在它确实存在时同步——凭空写一个 CropBox 会改变没有它的页面的语义。
    if "/CropBox" in page:
        crop = page.cropbox
        cwidth = float(crop.right) - float(crop.left)
        page.cropbox = RectangleObject(
            (
                float(crop.left),
                float(crop.bottom),
                float(crop.right) + cwidth * MARGIN_RATIO,
                float(crop.top),
            )
        )


def _add_outline(writer: PdfWriter, titles: list[str]) -> None:
    """每页一个书签。

    标题数与页数对不上时按较短的那个来：动画展开会让 PDF 页数多于原
    slide 数，而书签宁可少几条也不能指向不存在的页——那会让阅读器报错
    或者直接崩。
    """
    for index in range(min(len(titles), len(writer.pages))):
        writer.add_outline_item(titles[index], index)
