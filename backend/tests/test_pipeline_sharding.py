import pytest

from app.config import settings
from app.services.engine_router import select_engine
from app.services.pptx_probe import PptxMeta

MIB = 1024 * 1024


def _meta(pages: int) -> PptxMeta:
    return PptxMeta(
        slide_count=pages,
        slide_width_emu=12192000,
        slide_height_emu=6858000,
        fonts=(),
    )


def test_auto_routes_small_file_to_graph():
    assert select_engine(_meta(50), 10 * MIB) == "graph"


def test_auto_routes_long_deck_to_libreoffice():
    """自动判定不选切片路径：切片意味着数十次 HTTP 往返与几分钟等待，
    作为默认行为太重。"""
    assert select_engine(_meta(200), 10 * MIB) == "libreoffice"


def test_auto_routes_large_file_to_libreoffice():
    assert select_engine(_meta(50), 100 * MIB) == "libreoffice"


def test_explicit_request_always_wins():
    assert select_engine(_meta(500), 500 * MIB, requested="graph") == "graph"
    assert select_engine(_meta(5), 1 * MIB, requested="libreoffice") == "libreoffice"


def test_boundary_exactly_at_limits():
    assert select_engine(_meta(80), 40 * MIB) == "graph"
    assert select_engine(_meta(81), 40 * MIB) == "libreoffice"
