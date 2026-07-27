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


def test_auto_routes_small_file_to_graph_when_configured():
    """auto 分支只有在 Graph 已配置时才会选它——三期没有凭证写入路径，
    这里显式传 graph_configured=True 模拟"已配置"的场景（四期落地后）。"""
    assert select_engine(_meta(50), 10 * MIB, graph_configured=True) == "graph"


def test_auto_never_routes_to_graph_when_not_configured():
    """审查 I1：auto 分支不能选一个当前部署里必然不可用的引擎。三期没有
    凭证写入路径（管理页面是四期的事），graph_configured 默认 False，
    哪怕页数体积都在 Graph 阈值内也必须退回 libreoffice。"""
    assert select_engine(_meta(50), 10 * MIB) == "libreoffice"
    assert select_engine(_meta(50), 10 * MIB, graph_configured=False) == "libreoffice"


def test_auto_routes_long_deck_to_libreoffice():
    """自动判定不选切片路径：切片意味着数十次 HTTP 往返与几分钟等待，
    作为默认行为太重。"""
    assert select_engine(_meta(200), 10 * MIB, graph_configured=True) == "libreoffice"


def test_auto_routes_large_file_to_libreoffice():
    assert select_engine(_meta(50), 100 * MIB, graph_configured=True) == "libreoffice"


def test_explicit_request_always_wins():
    assert select_engine(_meta(500), 500 * MIB, requested="graph") == "graph"
    assert select_engine(_meta(5), 1 * MIB, requested="libreoffice") == "libreoffice"


def test_explicit_graph_request_wins_even_when_not_configured():
    """项目红线，钉死在 select_engine 这一层：graph_configured 只能影响
    auto 分支，绝不允许挪进 requested 分支。用户显式选了 graph 但未配置
    时，select_engine 必须原样返回 "graph"（让后续 get_engine 环节报
    GraphNotConfigured 明确失败），不能因为 graph_configured=False 就
    悄悄改成 libreoffice——那正是"静默降级"本身。"""
    assert (
        select_engine(_meta(5), 1 * MIB, requested="graph", graph_configured=False)
        == "graph"
    )


def test_boundary_exactly_at_limits():
    assert select_engine(_meta(80), 40 * MIB, graph_configured=True) == "graph"
    assert select_engine(_meta(81), 40 * MIB, graph_configured=True) == "libreoffice"
