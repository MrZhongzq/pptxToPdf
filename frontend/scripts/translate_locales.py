#!/usr/bin/env python3
"""从 en.json 机器翻译出其余语言。

用 Argos Translate（Apache-2.0）**在本地跑**，不调任何第三方 API。

为什么不用云翻译服务：这个脚本原本写的是 DeepL，而 DeepL 后来收紧了免费
API 的准入。一个只是用来补 UI 文案的辅助脚本，不该因为某家服务商改政策
就整条失效——本地模型跑一次装好就一直能用，也不需要在 CI 里配密钥。

中英两门是人工维护的，本脚本**不会覆盖它们**。

    python scripts/translate_locales.py            翻译缺失的条目
    python scripts/translate_locales.py --check    只校验对齐，不下模型
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LOCALES = Path(__file__).resolve().parent.parent / "src" / "i18n" / "locales"

# 人工维护，脚本不碰
SOURCE = {"en", "zh-CN"}

# 机器翻译的目标。键是语言文件名（BCP 47），值是 Argos 的语言代码——
# Argos 只有 pt，没有 pt-BR，文件名保留更精确的那个。
TARGETS = {
    "ja": "ja",
    "ko": "ko",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "ru": "ru",
    "pt-BR": "pt",
}

PLACEHOLDER = re.compile(r"\{(\w+)\}")

# 人工覆盖：模型对某些短语有稳定的坏输出，重跑多少次都一样。
#
# 实测例子：ko 的 engine.needsLogin 被译成「이름 *」（姓名 *）——那不是
# 随机波动，是这个模型对这条短语的固定错误。与其把整门语言判死，不如
# 就地钉一条正确的，其余仍交给模型。
#
# 加条目的判据：这条译文明显错、且重跑仍旧错。不要拿它做风格微调——
# 那会让人工维护面无声地扩散回来。
OVERRIDES: dict[str, dict[str, str]] = {
    "ko": {
        "engine.needsLogin": "로그인 필요",
    },
}


def load(name: str) -> dict[str, str]:
    path = LOCALES / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save(name: str, data: dict[str, str]) -> None:
    (LOCALES / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def check(en: dict[str, str]) -> int:
    """只比对 key 是否对齐。CI 的前端 job 跑这个——它不需要模型。"""
    failed = 0
    for name in TARGETS:
        current = load(name)
        missing = [k for k in en if k not in current]
        stale = [k for k in current if k not in en]
        if missing or stale:
            print(f"{name}: 缺 {len(missing)} 条，多余 {len(stale)} 条 —— 跑一次 translate_locales.py")
            failed = 1
        else:
            print(f"{name}: 与 en.json 对齐")
    return failed


def install_models() -> None:
    import argostranslate.package as pkg

    pkg.update_package_index()
    available = pkg.get_available_packages()
    installed = {(p.from_code, p.to_code) for p in pkg.get_installed_packages()}
    for argos_code in set(TARGETS.values()):
        if ("en", argos_code) in installed:
            continue
        match = next(
            (p for p in available if p.from_code == "en" and p.to_code == argos_code), None
        )
        if match is None:
            print(f"  Argos 没有 en->{argos_code} 的模型，跳过")
            continue
        print(f"  下载 en->{argos_code} …")
        pkg.install_from_path(match.download())


def translate_one(text: str, argos_code: str) -> str | None:
    """翻一条。译文里占位符没能原样保留时返回 None。

    实测 Argos 会原样保留 `{size}` 这种花括号占位符（不需要任何包裹标记，
    早先用 `__0__` 反而会被拆成 ` 0 `）。但那是观察不是保证——所以这里
    校验一遍：占位符对不上就宁可退回英文原文，也不能产出一条插值静默
    失效的文案（界面上会直接显示 `{size}` 或者干脆少一截）。
    """
    import argostranslate.translate as tr

    want = set(PLACEHOLDER.findall(text))
    out = tr.translate(text, "en", argos_code)
    if set(PLACEHOLDER.findall(out)) != want:
        return None
    return out


def main() -> int:
    en = load("en")
    if not en:
        print("找不到 en.json", file=sys.stderr)
        return 1

    if "--check" in sys.argv:
        return check(en)

    print("准备模型…")
    install_models()

    for name, argos_code in TARGETS.items():
        current = load(name)
        missing = [k for k in en if k not in current]
        merged: dict[str, str] = {}
        kept_english = 0

        for key in en:  # 按 en.json 的顺序重排，顺带丢掉已删除的 key
            if key in current and key not in missing:
                merged[key] = current[key]
                continue
            override = OVERRIDES.get(name, {}).get(key)
            if override is not None:
                merged[key] = override
                continue
            translated = translate_one(en[key], argos_code)
            if translated is None:
                merged[key] = en[key]
                kept_english += 1
            else:
                merged[key] = translated

        save(name, merged)
        note = f"，其中 {kept_english} 条占位符没保住、退回英文" if kept_english else ""
        print(f"{name}: 新翻 {len(missing)} 条{note}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
