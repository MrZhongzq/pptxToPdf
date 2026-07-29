import pytest
from fastapi.testclient import TestClient
from pptx import Presentation
from pptx.util import Emu

from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Base/engine 延迟到函数体内导入，理由见 test_uploads_api.py 里同名 fixture
    # 的注释：conftest.py 的 _isolate_app_db autouse fixture 重定向了
    # app.db.engine，模块顶层 import 会绕过这个重定向。
    from app.db import Base, engine

    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "chunk_size", 64 * 1024)
    settings.ensure_dirs()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from app.main import app

    with TestClient(app) as c:
        yield c


def _sample_deck_bytes() -> bytes:
    import io

    prs = Presentation()
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(6858000)
    for _ in range(4):
        prs.slides.add_slide(prs.slide_layouts[6])
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _upload_a_deck(client, engine: str | None = None) -> str:
    """走完整的分块上传协议，落地一个 ready 状态的任务，返回 task_id。

    engine：透传给 POST /api/uploads 的 `engine` 字段（用户上传时选的引擎，
    未废弃，参见 CreateUploadRequest）。默认 None，即不指定。
    """
    payload = _sample_deck_bytes()
    body = {"filename": "deck.pptx", "size": len(payload)}
    if engine is not None:
        body["engine"] = engine
    created = client.post("/api/uploads", json=body).json()
    uid, size = created["upload_id"], created["chunk_size"]
    for idx in range(created["total_chunks"]):
        client.put(
            f"/api/uploads/{uid}/chunks/{idx}",
            content=payload[idx * size : (idx + 1) * size],
        )
    return client.post(f"/api/uploads/{uid}/complete").json()["task_id"]


def _session():
    import app.db as db_module

    return db_module.SessionLocal()


def _get_task(task_id: str) -> dict:
    """走真实的 get_task 端点函数（不经 HTTP 层），拿到与 API 响应同构的 dict。"""
    from app.api.tasks import get_task as _get_task_endpoint

    session = _session()
    try:
        return _get_task_endpoint(task_id, session).model_dump()
    finally:
        session.close()


def _load_task_row(task_id: str):
    from app.models import Task

    session = _session()
    try:
        return session.get(Task, task_id)
    finally:
        session.close()


def test_complete_leaves_task_ready_and_does_not_enqueue(client, monkeypatch):
    """complete 只拼装落库，不入队。

    fix round：uploads.py 已经不再 import enqueue_conversion（谁在
    complete_upload 里写 enqueue_conversion(task_id) 现在会直接 NameError，
    比一条测试更硬）。monkeypatch.setattr 默认要求属性已存在，这里用
    raising=False 让它在 app.api.uploads 的模块 globals 上新建这个名字——
    如果生产代码真被改回去调用它，运行时依然会解析到这个假替身并被
    enqueued 断言抓住；不加 raising=False 则会在 setattr 这一步就
    AttributeError，反而测不到「加回调用」这个变异。
    """
    enqueued = []
    monkeypatch.setattr(
        "app.api.uploads.enqueue_conversion",
        lambda t: enqueued.append(t),
        raising=False,
    )
    task_id = _upload_a_deck(client)
    task = _get_task(task_id)
    assert task["status"] == "ready"
    assert enqueued == [], "complete 不该入队"


def test_start_enqueues_and_moves_to_pending(client, monkeypatch):
    enqueued = []
    monkeypatch.setattr("app.api.tasks.enqueue_conversion", lambda t: enqueued.append(t))
    task_id = _upload_a_deck(client)
    resp = client.post(f"/api/tasks/{task_id}/start", json={"engine": "libreoffice"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert enqueued == [task_id]


def test_start_records_engine_and_options(client, monkeypatch):
    """引擎与选项在 start 时才定——这正是本期的目的。"""
    monkeypatch.setattr("app.api.tasks.enqueue_conversion", lambda t: None)
    task_id = _upload_a_deck(client)
    client.post(
        f"/api/tasks/{task_id}/start",
        json={"engine": "graph", "options": {"expand_animations": True}},
    )
    task = _load_task_row(task_id)
    assert task.requested_engine == "graph"
    assert "expand_animations" in (task.options_json or "")


def test_start_without_engine_keeps_the_one_chosen_at_upload(client, monkeypatch):
    """fix round I1：上传时选了引擎、start 不带 engine 时不该被静默清空。

    complete_upload 把 upload.requested_engine 转写进 task.requested_engine；
    start 之前它是无条件覆盖成 payload.engine（默认 None），上传时选的引擎
    会在没人碰它的情况下消失。用户裁决沿用上传时选的——start 只在
    payload.engine 非 None 时才覆盖。
    """
    monkeypatch.setattr("app.api.tasks.enqueue_conversion", lambda t: None)
    task_id = _upload_a_deck(client, engine="graph")
    client.post(f"/api/tasks/{task_id}/start", json={})
    task = _load_task_row(task_id)
    assert task.requested_engine == "graph"


def test_start_twice_is_409(client, monkeypatch):
    enqueued = []
    monkeypatch.setattr("app.api.tasks.enqueue_conversion", lambda t: enqueued.append(t))
    task_id = _upload_a_deck(client)
    client.post(f"/api/tasks/{task_id}/start", json={})
    resp = client.post(f"/api/tasks/{task_id}/start", json={})
    assert resp.status_code == 409
    assert resp.json()["code"] == "TASK_ALREADY_STARTED"
    assert len(enqueued) == 1, "不该重复入队"


def test_start_on_already_reaped_ready_task_is_410(client, monkeypatch):
    """purge_expired_ready 已经把这个任务标 failed + READY_EXPIRED 之后，
    /start 必须把「已被回收」和「真的已经在跑」（test_start_twice_is_409）
    区分开——前者是 410，不是笼统的 409。message 复用回收器自己写的那句，
    不在 start_task 里另起一份，两处措辞才不会跑偏。"""
    from datetime import datetime, timedelta, timezone

    from app.models import Task
    from app.services.retention import purge_expired_ready

    task_id = _upload_a_deck(client)

    session = _session()
    try:
        row = session.get(Task, task_id)
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            hours=settings.ready_ttl_hours, minutes=1
        )
        session.commit()
    finally:
        session.close()

    assert purge_expired_ready() == 1
    expected_message = _load_task_row(task_id).error_message

    resp = client.post(f"/api/tasks/{task_id}/start", json={})
    assert resp.status_code == 410
    assert resp.json()["code"] == "READY_EXPIRED"
    assert resp.json()["message"] == expected_message


def test_start_on_missing_task_is_404(client):
    resp = client.post("/api/tasks/does-not-exist/start", json={})
    assert resp.status_code == 404
    assert resp.json()["code"] == "TASK_NOT_FOUND"


def test_start_drops_original_when_enqueue_fails(client, monkeypatch):
    """Redis 挂了时必须删原文件——这段兜底是从 complete 挪过来的，
    漏挪的话每次 Redis 抖动都留一份 80-500MB 的孤儿。"""
    dropped = []
    monkeypatch.setattr("app.api.tasks.drop_original", lambda t: dropped.append(t))

    def boom(_):
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.api.tasks.enqueue_conversion", boom)

    task_id = _upload_a_deck(client)
    resp = client.post(f"/api/tasks/{task_id}/start", json={})
    assert resp.status_code == 503
    assert dropped == [task_id]
    assert _load_task_row(task_id).status == "failed"
