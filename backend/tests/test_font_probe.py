"""fc-query 输出的解析。

用固定的样本输出做单测，不依赖真实字体文件——真字体体积大且有版权。
样本取自真机上 fontconfig 2.14 的实际输出。
"""

import sys
from unittest.mock import MagicMock

# Mock 外部依赖以便全局 conftest 能导入 app.services.pipeline（即使本测试不需要它）。
sys.modules['redis'] = MagicMock()
sys.modules['rq'] = MagicMock()
sys.modules['rq.job'] = MagicMock()

from app.services.font_probe import FontFace, format_version, parse_charset, parse_fc_query


def test_parses_single_face_ttf() -> None:
    stdout = "DejaVu Sans\tBook\t155320\t0\n"
    assert parse_fc_query(stdout) == [
        FontFace(family="DejaVu Sans", style="Book", version="2.37", index=0)
    ]


def test_parses_every_face_of_a_ttc() -> None:
    """ttc 是字体集合，一个文件多个 face。微软雅黑与 Noto CJK 都是这种。

    冲突判定要看全部 face，漏一个就会出现「明明撞了却没提示」。
    """
    stdout = (
        "Noto Sans CJK JP\tBold\t131072\t0\n"
        "Noto Sans CJK KR\tBold\t131072\t1\n"
        "Noto Sans CJK SC\tBold\t131072\t2\n"
    )
    faces = parse_fc_query(stdout)
    assert [f.family for f in faces] == [
        "Noto Sans CJK JP",
        "Noto Sans CJK KR",
        "Noto Sans CJK SC",
    ]
    assert [f.index for f in faces] == [0, 1, 2]


def test_ignores_blank_and_malformed_lines() -> None:
    """fc-query 偶尔会在末尾多输出空行；字段不足的行直接跳过而不是抛错,
    否则一个畸形 face 会让整个文件不可用。"""
    stdout = "DejaVu Sans\tBook\t155320\t0\n\n坏行\n"
    assert len(parse_fc_query(stdout)) == 1


def test_version_is_fixed_point_divided_by_65536() -> None:
    """fontversion 是 16.16 定点数，直接显示会变成 155320 这种天书。"""
    assert format_version("155320") == "2.37"
    assert format_version("131072") == "2.00"


def test_version_falls_back_to_dash_when_unparsable() -> None:
    assert format_version("") == "-"
    assert format_version("abc") == "-"


def test_counts_codepoints_from_charset_ranges() -> None:
    """charset 是十六进制范围列表，空格分隔，每项是 start-end 或单个码位。

    这个数字比版本号有用得多：版本号看不出子集化，字数能。
    """
    # 20-7e = 95 个, a0 = 1 个, 300-302 = 3 个
    assert parse_charset("20-7e a0 300-302") == 99


def test_empty_charset_counts_zero() -> None:
    assert parse_charset("") == 0
