import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.errors import ConversionFailed
from app.services.pdf_merge import merge_pdfs


def _make_pdf(path, labels):
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for label in labels:
        pdf.setFont("Helvetica", 40)
        pdf.drawCentredString(300, 400, label)
        pdf.showPage()
    pdf.save()
    return path


def _texts(path):
    return [(p.extract_text() or "").strip() for p in PdfReader(str(path)).pages]


def test_merge_preserves_order(tmp_path):
    """页序是三期最危险的失败模式：顺序错了页数完全正确，任何页数
    校验都抓不到，用户可能翻到考前才发现第 30 页跑到了第 10 页。"""
    a = _make_pdf(tmp_path / "a.pdf", ["P1", "P2", "P3"])
    b = _make_pdf(tmp_path / "b.pdf", ["P4", "P5"])
    c = _make_pdf(tmp_path / "c.pdf", ["P6"])

    dest = tmp_path / "merged.pdf"
    pages = merge_pdfs([a, b, c], dest)

    assert pages == 6
    assert _texts(dest) == ["P1", "P2", "P3", "P4", "P5", "P6"]


def test_merge_respects_given_order_not_filename(tmp_path):
    """合并顺序必须由入参决定，不能依赖文件名排序——分片文件名是
    000/001/002，一旦有人改成按 glob 排序，10 片时会变成 0,1,10,2…"""
    a = _make_pdf(tmp_path / "z_first.pdf", ["P1"])
    b = _make_pdf(tmp_path / "a_second.pdf", ["P2"])
    dest = tmp_path / "merged.pdf"

    merge_pdfs([a, b], dest)
    assert _texts(dest) == ["P1", "P2"]


def test_merge_ten_shards_numeric_filename_trap(tmp_path):
    """字典序陷阱的直接复现：shard10.pdf 在字典序下排在 shard2.pdf 前面。
    传入顺序是 shard0..shard10 共 11 片，若实现依赖文件名排序而非入参
    顺序，页序会被打乱成 0,1,10,2,3...——本用例逐页断言标签能抓住它。"""
    paths = []
    labels = []
    for i in range(11):
        label = f"P{i}"
        labels.append(label)
        paths.append(_make_pdf(tmp_path / f"shard{i}.pdf", [label]))

    dest = tmp_path / "merged.pdf"
    pages = merge_pdfs(paths, dest)

    assert pages == 11
    assert _texts(dest) == labels


def test_merge_single_part(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["ONLY"])
    dest = tmp_path / "merged.pdf"
    assert merge_pdfs([a], dest) == 1
    assert _texts(dest) == ["ONLY"]


def test_missing_part_raises(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    with pytest.raises(ConversionFailed) as exc:
        merge_pdfs([a, tmp_path / "nope.pdf"], tmp_path / "merged.pdf")
    assert exc.value.code == "CONVERSION_FAILED"


def test_corrupt_part_raises(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(ConversionFailed):
        merge_pdfs([a, bad], tmp_path / "merged.pdf")


def test_empty_parts_raises(tmp_path):
    with pytest.raises(ConversionFailed):
        merge_pdfs([], tmp_path / "merged.pdf")


def test_failed_merge_leaves_no_partial_output(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    dest = tmp_path / "merged.pdf"
    with pytest.raises(ConversionFailed):
        merge_pdfs([a, bad], dest)
    # 半个合并结果比没有更糟——后续的页数校验会拿它当有效产物
    assert not dest.exists()
