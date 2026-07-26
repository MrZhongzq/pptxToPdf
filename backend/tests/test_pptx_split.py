import io
import random
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Emu, Inches

from app.services.pptx_probe import probe
from app.services.pptx_split import split_pptx

SLIDES = 8


def _noisy_png(w=700, h=450) -> io.BytesIO:
    """随机噪声图压缩率低，单张几百 KB——用来验证 media 确实被裁掉。"""
    img = Image.new("RGB", (w, h))
    img.putdata(
        [
            (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for _ in range(w * h)
        ]
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


@pytest.fixture
def deck(tmp_path) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
    for i in range(SLIDES):
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = f"PAGE-{i + 1}"
        s.shapes.add_picture(_noisy_png(), Inches(1), Inches(2), width=Inches(4))
    path = tmp_path / "deck.pptx"
    prs.save(path)
    return path


def _media_count(path: Path) -> int:
    with zipfile.ZipFile(path) as z:
        return len([n for n in z.namelist() if n.startswith("ppt/media/")])


def test_split_page_counts(deck, tmp_path):
    out = split_pptx(deck, [(1, 3), (4, 8)], tmp_path / "shards")
    assert len(out) == 2
    assert probe(out[0]).slide_count == 3
    assert probe(out[1]).slide_count == 5


def test_split_drops_unreferenced_media(deck, tmp_path):
    assert _media_count(deck) == SLIDES
    out = split_pptx(deck, [(1, 3), (4, 8)], tmp_path / "shards")
    # 关键断言：只从 sldIdLst 删除而不 drop_rel 的实现会让所有 media
    # 留在包里，这里就会看到 8 而不是 3/5。
    assert _media_count(out[0]) == 3
    assert _media_count(out[1]) == 5


def test_split_reduces_file_size(deck, tmp_path):
    out = split_pptx(deck, [(1, 4), (5, 8)], tmp_path / "shards")
    total = sum(p.stat().st_size for p in out)
    # 两片各占一半 media，合计应明显小于原包的 1.5 倍
    # （共享的 theme/master 会在每片里各留一份，所以不是精确的 1.0 倍）
    assert total < deck.stat().st_size * 1.5


def test_split_preserves_page_order_and_content(deck, tmp_path):
    """切片后每页的标题必须还是原来那一页的标题，顺序不能乱。"""
    out = split_pptx(deck, [(1, 3), (4, 8)], tmp_path / "shards")

    first = Presentation(str(out[0]))
    assert [s.shapes.title.text for s in first.slides] == [
        "PAGE-1",
        "PAGE-2",
        "PAGE-3",
    ]
    second = Presentation(str(out[1]))
    assert [s.shapes.title.text for s in second.slides] == [
        "PAGE-4",
        "PAGE-5",
        "PAGE-6",
        "PAGE-7",
        "PAGE-8",
    ]


def test_split_output_is_valid_pptx(deck, tmp_path):
    """产出的包必须能被 python-pptx 打开、被 pptx_probe 解析。"""
    out = split_pptx(deck, [(2, 5)], tmp_path / "shards")
    meta = probe(out[0])
    assert meta.slide_count == 4
    assert meta.slide_width_emu == 12192000
    prs = Presentation(str(out[0]))
    assert len(prs.slides) == 4


def test_single_page_range(deck, tmp_path):
    out = split_pptx(deck, [(5, 5)], tmp_path / "shards")
    assert probe(out[0]).slide_count == 1
    assert Presentation(str(out[0])).slides[0].shapes.title.text == "PAGE-5"


def test_full_range_is_a_faithful_copy(deck, tmp_path):
    out = split_pptx(deck, [(1, SLIDES)], tmp_path / "shards")
    assert probe(out[0]).slide_count == SLIDES
    assert _media_count(out[0]) == SLIDES


def test_rejects_out_of_bounds_range(deck, tmp_path):
    with pytest.raises(ValueError):
        split_pptx(deck, [(1, SLIDES + 1)], tmp_path / "shards")
    with pytest.raises(ValueError):
        split_pptx(deck, [(0, 3)], tmp_path / "shards")
