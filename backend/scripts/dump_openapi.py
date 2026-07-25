"""导出 OpenAPI 契约快照。

二三期把占位引擎换成 LibreOffice / Graph 时，重跑本脚本后 git diff 应当为空——
若有差异，说明改动破坏了前端依赖的契约。
"""

import json
from pathlib import Path

from app.main import app

OUT = Path(__file__).resolve().parents[1] / "openapi.json"


def main() -> None:
    OUT.write_text(
        json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
