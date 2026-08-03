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

#: 取字符集用的格式。**必须带 \n**——ttc 一个文件多个 face，fc-query 会为
#: 每个 face 各输出一段，没有分隔符的话相邻两段会粘成一个畸形 token
#: （前段末尾 ...ffff 接后段开头 20-7e 变成 ffff20-7e），算出来是大负数。
#: 真机上一个 10 face 的 Noto CJK 因此显示「覆盖 -462,222,757 字」。
CHARSET_FORMAT = "%{charset}\n"


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
            # index 畸形就回退成 0，而不是丢掉整行。index 不参与冲突判定（那只看
            # family），标错了没有后果；但漏掉一个 face 就让 ttc 里的某个字体名彻底
            # 消失，后续出现「明明撞名了却没提示」，用户的字体被悄悄覆盖。两害相权，
            # 宁可标错也不能丢。
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

    每一段输入形如 "20-7e a0-2e9 2f3"，十六进制，空格分隔，每项是范围或
    单点。看不懂的项跳过——宁可少算几个字，也不要因为一个畸形范围就报错。

    ttc 一个文件多个 face，用 CHARSET_FORMAT（带 \n）调用 fc-query 时会
    按 face 换行分段。多段时取覆盖最多的一段代表整个文件，**不求和**：
    ttc 里的各 face 通常是同一字体针对不同语言区域的变体，字符集高度
    重叠（真机实测一个 10 face 的 Noto CJK，每段都是 44810 字），求和
    会把覆盖量算成 10 倍，而这个数字的唯一用途是让管理员比较两个字体谁
    更全，虚高 10 倍就是误导。取最大值代表「这个文件最全的那个 face
    覆盖多少」，用于判断子集化是否足够。
    """
    best = 0
    for segment in raw.splitlines():
        count = 0
        for token in segment.split():
            try:
                if "-" in token:
                    start, end = token.split("-", 1)
                    start_v = int(start, 16)
                    end_v = int(end, 16)
                    if end_v < start_v:
                        # 畸形范围：多半是忘记加分隔符时，前一段末尾的
                        # ...ffff 跟下一段开头的 20-7e 粘连产生的。这种
                        # token 算出来是负数，本函数永远不该返回负数，
                        # 所以在这里直接跳过，而不是让它拖累整体计数。
                        continue
                    count += end_v - start_v + 1
                else:
                    int(token, 16)
                    count += 1
            except ValueError:
                continue
        best = max(best, count)
    return best
