import pytest
from pypdf import PdfReader

from app.services.engines import get_engine
from app.services.engines.base import EMU_PER_POINT
from app.services.engines.placeholder import PlaceholderEngine
from app.services.pptx_probe import PptxMeta


@pytest.fixture
def meta():
    return PptxMeta(
        slide_count=3,
        slide_width_emu=12192000,
        slide_height_emu=6858000,
        fonts=("Calibri",),
    )


def test_placeholder_page_count(tmp_path, meta):
    dest = tmp_path / "out.pdf"
    PlaceholderEngine().convert(tmp_path / "unused.pptx", meta, dest)

    assert len(PdfReader(dest).pages) == 3


def test_placeholder_page_size_matches_slide(tmp_path, meta):
    dest = tmp_path / "out.pdf"
    PlaceholderEngine().convert(tmp_path / "unused.pptx", meta, dest)

    page = PdfReader(dest).pages[0]
    expected_w = meta.slide_width_emu / EMU_PER_POINT
    expected_h = meta.slide_height_emu / EMU_PER_POINT
    assert float(page.mediabox.width) == pytest.approx(expected_w, abs=1.0)
    assert float(page.mediabox.height) == pytest.approx(expected_h, abs=1.0)


def test_placeholder_has_selectable_text(tmp_path, meta):
    dest = tmp_path / "out.pdf"
    PlaceholderEngine().convert(tmp_path / "unused.pptx", meta, dest)

    text = PdfReader(dest).pages[0].extract_text()
    assert "1 / 3" in text


def test_get_engine_returns_placeholder():
    assert get_engine("placeholder").name == "placeholder"


def test_get_engine_unknown_raises():
    with pytest.raises(KeyError):
        get_engine("libreoffice")
