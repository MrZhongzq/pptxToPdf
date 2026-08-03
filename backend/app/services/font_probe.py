"""从字体文件里读出 family / style / 版本 / 覆盖字数。

用 fontconfig 自带的 fc-query，不引入 fontTools——镜像里已经装了
fontconfig（LibreOffice 依赖它），多一个 Python 依赖就多一份构建风险。

本模块只做纯解析，不碰文件系统，方便单测。
"""

from __future__ import annotations

from dataclasses import dataclass

#: fc-query 的输出格式。字段用 tab 分隔——family 里可能有空格（"DejaVu Sans"），
#: 用空格分隔会切错。
FC_QUERY_FORMAT = "%{family}\t%{style}\t%{fontversion}\t%{index}\n"


@dataclass(frozen=True)
class FontFace:
    """字体文件里的一个 face。

    ttc 一个文件含多个 face，所以 face 与文件是多对一。
    """

    family: str
    style: str
    version: str
    index: int


def format_version(raw: str) -> str:
    """fontversion 是 16.16 定点数，除以 65536 才是人看的版本号。

    155320 → "2.37"。解析不了就给一个横杠，不要让整个列表崩掉——
    有些字体的这个字段是空的。
    """
    try:
        return f"{int(raw) / 65536:.2f}"
    except (TypeError, ValueError):
        return "-"


def parse_fc_query(stdout: str) -> list[FontFace]:
    """把 fc-query 的多行输出解析成 face 列表。

    字段不足的行直接跳过而不是抛错：一个畸形 face 不该让整个文件不可用。
    """
    faces: list[FontFace] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        family, style, version, index = parts[0], parts[1], parts[2], parts[3]
        if not family.strip():
            continue
        try:
            idx = int(index)
        except ValueError:
            idx = 0
        faces.append(
            FontFace(
                family=family.strip(),
                style=style.strip(),
                version=format_version(version),
                index=idx,
            )
        )
    return faces


def parse_charset(raw: str) -> int:
    """数出 charset 覆盖多少个码位。

    输入形如 "20-7e a0-2e9 2f3"，十六进制，空格分隔，每项是范围或单点。
    看不懂的项跳过——宁可少算几个字，也不要因为一个畸形范围就报错。
    """
    total = 0
    for token in raw.split():
        try:
            if "-" in token:
                start, end = token.split("-", 1)
                total += int(end, 16) - int(start, 16) + 1
            else:
                int(token, 16)
                total += 1
        except ValueError:
            continue
    return total
