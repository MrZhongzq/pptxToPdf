"""守住 openapi.json 与实际路由的一致性。

三期 Task 8 曾漏过一次快照重新生成，靠人工执行才发现——仓库当时
对此没有任何自动守卫。
"""

import json
from pathlib import Path

from app.main import app

SNAPSHOT = Path(__file__).resolve().parent.parent / "openapi.json"


def test_openapi_snapshot_is_current():
    current = app.openapi()
    stored = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert stored == current, (
        "openapi.json 与当前路由不一致。"
        "在 backend/ 下跑 .venv/Scripts/python.exe -m scripts.dump_openapi 重新生成。"
    )
