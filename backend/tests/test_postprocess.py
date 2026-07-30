"""转换后处理：书签大纲与页边距重映射。

按六期 spec 的测试要求，每条都断言「产出的 PDF 能被 pypdf 重新打开且
结构正确」，而不只是断言函数被调用过——后处理是就地重写整份 PDF，
写坏了的表现是用户下载到一个打不开的文件。
"""

import pytest
from pypdf import PdfReader, PdfWriter

from app.schemas import ConversionOptions
from app.services import postprocess
from app.services.postprocess import MARGIN_RATIO, apply

PAGE_W, PAGE_H = 720.0, 405.0  # 16:9，与 13.3"x7.5" 课件同比例


@pytest.fixture
def pdf(tmp_path):
    """造一份 3 页的 PDF。"""
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=PAGE_W, height=PAGE_H)
    path = tmp_path / "out.pdf"
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def _opts(**kw) -> ConversionOptions:
    return ConversionOptions(**kw)


# ---- 不勾选时不碰文件 ----


def test_no_options_leaves_file_untouched(pdf):
    """重写一遍 PDF 不是零成本，而且 pypdf 往返会丢掉它不认识的对象。
    一项都没勾时必须原样不动。"""
    before = pdf.read_bytes()
    assert apply(pdf, _opts()) is False
    assert pdf.read_bytes() == before


def test_outline_without_titles_is_a_noop(pdf):
    """勾了书签但标题提取失败（空列表）——不该产出一份没有书签的重写文件，
    直接不碰。"""
    before = pdf.read_bytes()
    assert apply(pdf, _opts(pdf_outline=True), titles=[]) is False
    assert pdf.read_bytes() == before


# ---- 书签 ----


def test_outline_adds_one_bookmark_per_page(pdf):
    assert apply(pdf, _opts(pdf_outline=True), titles=["甲", "乙", "丙"]) is True

    reader = PdfReader(str(pdf))
    assert len(reader.outline) == 3
    assert [o.title for o in reader.outline] == ["甲", "乙", "丙"]


def test_outline_survives_reopen(pdf):
    """产出的必须是一份能被重新打开的合法 PDF。"""
    apply(pdf, _opts(pdf_outline=True), titles=["A", "B", "C"])
    reader = PdfReader(str(pdf))
    assert len(reader.pages) == 3


def test_outline_points_to_correct_pages(pdf):
    apply(pdf, _opts(pdf_outline=True), titles=["P1", "P2", "P3"])
    reader = PdfReader(str(pdf))
    for i, item in enumerate(reader.outline):
        assert reader.get_destination_page_number(item) == i


def test_more_titles_than_pages_is_truncated(pdf):
    """动画展开会让页数与原 slide 数不一致。书签宁可少几条也不能指向
    不存在的页——那会让阅读器报错甚至崩。"""
    apply(pdf, _opts(pdf_outline=True), titles=["1", "2", "3", "4", "5"])
    assert len(PdfReader(str(pdf)).outline) == 3


def test_fewer_titles_than_pages_is_fine(pdf):
    apply(pdf, _opts(pdf_outline=True), titles=["只有一个"])
    assert len(PdfReader(str(pdf)).outline) == 1


# ---- 页边距 ----


def test_margins_widen_the_page(pdf):
    assert apply(pdf, _opts(remap_margins=True)) is True

    page = PdfReader(str(pdf)).pages[0]
    width = float(page.mediabox.right) - float(page.mediabox.left)
    assert width == pytest.approx(PAGE_W * (1 + MARGIN_RATIO))


def test_margins_do_not_change_height(pdf):
    """只往右扩宽。改高度会让版面比例失真。"""
    apply(pdf, _opts(remap_margins=True))
    page = PdfReader(str(pdf)).pages[0]
    assert float(page.mediabox.top) - float(page.mediabox.bottom) == pytest.approx(PAGE_H)


def test_margins_extend_to_the_right_not_the_left(pdf):
    """留白必须加在右边：左边界一动，内容就跟着挪，等于把整页往右推。"""
    before = PdfReader(str(pdf)).pages[0].mediabox
    apply(pdf, _opts(remap_margins=True))
    after = PdfReader(str(pdf)).pages[0].mediabox

    assert float(after.left) == pytest.approx(float(before.left))
    assert float(after.right) > float(before.right)


def test_margins_apply_to_every_page(pdf):
    apply(pdf, _opts(remap_margins=True))
    for page in PdfReader(str(pdf)).pages:
        width = float(page.mediabox.right) - float(page.mediabox.left)
        assert width == pytest.approx(PAGE_W * (1 + MARGIN_RATIO))


def test_margins_sync_cropbox_when_present(tmp_path):
    """阅读器按 CropBox 显示。只扩 MediaBox 而不动 CropBox 的话，用户
    根本看不到多出来的空白。"""
    writer = PdfWriter()
    writer.add_blank_page(width=PAGE_W, height=PAGE_H)
    writer.pages[0].cropbox = writer.pages[0].mediabox
    path = tmp_path / "c.pdf"
    with path.open("wb") as fh:
        writer.write(fh)

    apply(path, _opts(remap_margins=True))

    page = PdfReader(str(path)).pages[0]
    cwidth = float(page.cropbox.right) - float(page.cropbox.left)
    assert cwidth == pytest.approx(PAGE_W * (1 + MARGIN_RATIO))


# ---- 组合 ----


def test_both_options_together(pdf):
    assert apply(pdf, _opts(pdf_outline=True, remap_margins=True), titles=["甲", "乙", "丙"]) is True

    reader = PdfReader(str(pdf))
    assert len(reader.outline) == 3
    width = float(reader.pages[0].mediabox.right) - float(reader.pages[0].mediabox.left)
    assert width == pytest.approx(PAGE_W * (1 + MARGIN_RATIO))
    assert len(reader.pages) == 3


def test_failure_leaves_original_intact(pdf, monkeypatch):
    """写坏了不能留下半份文件——用户手里那份必须要么是旧的、要么是新的。"""
    before = pdf.read_bytes()

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(PdfWriter, "write", boom)
    with pytest.raises(OSError):
        apply(pdf, _opts(remap_margins=True))

    assert pdf.read_bytes() == before
    assert not (pdf.with_suffix(".pp.pdf")).exists()
