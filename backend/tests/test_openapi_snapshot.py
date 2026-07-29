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


def test_start_endpoint_declares_503_engine_unavailable():
    """终审 I-1：tasks.py::start_task 明确 raise 了 EngineUnavailable（入队
    失败的兜底，从 complete_upload 整体挪过来的），但共享的 TASK_ERRORS
    只声明了 404/409/410——契约里从未声明过 503，这是搬代码时漏掉的。"""
    responses = app.openapi()["paths"]["/api/tasks/{task_id}/start"]["post"]["responses"]
    assert "503" in responses


def test_get_task_only_declares_codes_it_can_actually_raise():
    """get_task 只会抛 TaskNotFound（404）。共享 TASK_ERRORS 曾经让它
    一并声明 start 专属的 TASK_ALREADY_STARTED(409) / READY_EXPIRED(410)
    ——两者 get_task 从不抛，前端若照契约给这个端点写这些分支就是死代码。"""
    responses = app.openapi()["paths"]["/api/tasks/{task_id}"]["get"]["responses"]
    assert set(responses) & {"200", "404"} == {"200", "404"}
    assert "409" not in responses
    assert "410" not in responses


def test_download_only_declares_codes_it_can_actually_raise():
    """download 只会抛 TaskNotFound(404) / TaskNotReady(409) /
    ResultExpired(410)——从不抛 TASK_ALREADY_STARTED 或 READY_EXPIRED，
    共享 TASK_ERRORS 曾经把它们也搭在这个端点上。"""
    responses = app.openapi()["paths"]["/api/tasks/{task_id}/download"]["get"]["responses"]
    assert "409" in responses
    assert "READY_EXPIRED" not in json.dumps(responses)
    assert "TASK_ALREADY_STARTED" not in json.dumps(responses)
