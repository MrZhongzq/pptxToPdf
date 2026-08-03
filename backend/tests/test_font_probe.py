"""fc-query 输出的解析。

用固定的样本输出做单测，不依赖真实字体文件——真字体体积大且有版权。
样本取自真机上 fontconfig 2.14 的实际输出。
"""

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


def test_keeps_the_face_when_only_the_index_is_malformed() -> None:
    """index 畸形就回退成 0，而不是丢掉整个 face。

    index 不参与冲突判定（那只看 family），标错了没有后果；但丢掉一个
    face 就会让 ttc 里的某个字体名彻底消失，后续出现「明明撞名了却没
    提示」，用户的字体被悄悄覆盖。两害相权，宁可标错也不能丢。
    """
    faces = parse_fc_query("微软雅黑\tRegular\t393216\t不是数字\n")
    assert len(faces) == 1
    assert faces[0].family == "微软雅黑"
    assert faces[0].index == 0


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


def test_charset_multi_segment_takes_the_max() -> None:
    """ttc 一个文件多个 face，CHARSET_FORMAT 按 \\n 给每个 face 分一段。
    各段字数不一样时取覆盖最多的一段代表整个文件，而不是求和——见
    parse_charset 里的注释。"""
    raw = "0-9\n0-63\n0-1\n"  # 10 个, 100 个, 2 个
    assert parse_charset(raw) == 100


def test_charset_multi_segment_all_same_returns_that_value() -> None:
    """真机实测：10 face 的 NotoSansCJK-Bold.ttc 里每个 face 的字符集都是
    44810 字（同一字体针对不同语言区域的变体，字符集高度重叠）。取最大值
    而不是求和，避免把这个文件的覆盖量算成 448100——虚高 10 倍，误导
    管理员判断这个文件是否已经足够全。
    """
    segment = "0-af09"  # 0x0-0xaf09 = 44810 个码位
    raw = "\n".join([segment] * 10)
    assert parse_charset(raw) == 44810


def test_charset_malformed_glued_range_does_not_go_negative() -> None:
    """CHARSET_FORMAT 忘记加 \\n 时，前一段末尾 ...ffff 会跟下一段开头
    20-7e 粘成 "ffff20-7e" 这种 end < start 的畸形 token。真机上这种
    粘连让「覆盖字数」显示成 -462,222,757。parse_charset 永远不该返回
    负数——这道防御独立于「有没有分隔符」，即使将来又冒出别的畸形输入
    也不能把负数摆到界面上。
    """
    assert parse_charset("ffff20-7e") >= 0
