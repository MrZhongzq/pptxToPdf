import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import Base, SessionLocal, engine


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "chunk_size", 4)  # 小块便于测试
    settings.ensure_dirs()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create(client, size: int, filename="deck.pptx"):
    resp = await client.post(
        "/api/uploads", json={"filename": filename, "size": size}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_create_computes_total_chunks(client):
    body = await _create(client, size=10)  # 10 字节 / 4 字节块 = 3 块
    assert body["total_chunks"] == 3
    assert body["chunk_size"] == 4


async def test_create_rejects_oversize(client):
    resp = await client.post(
        "/api/uploads",
        json={"filename": "huge.pptx", "size": settings.max_file_size + 1},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "UPLOAD_SIZE_EXCEEDED"


async def test_status_reports_received_indices(client):
    body = await _create(client, size=10)
    uid = body["upload_id"]

    await client.put(f"/api/uploads/{uid}/chunks/2", content=b"gh")
    await client.put(f"/api/uploads/{uid}/chunks/0", content=b"abcd")

    resp = await client.get(f"/api/uploads/{uid}")
    assert resp.json()["received_indices"] == [0, 2]
    assert resp.json()["bytes_received"] == 6
    assert resp.json()["chunk_size"] == 4  # 客户端续传时靠它切分，不得自行反推


async def test_status_unknown_upload(client):
    resp = await client.get("/api/uploads/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["code"] == "UPLOAD_SESSION_NOT_FOUND"


async def test_complete_with_missing_chunk(client):
    body = await _create(client, size=10)
    uid = body["upload_id"]
    await client.put(f"/api/uploads/{uid}/chunks/0", content=b"abcd")

    resp = await client.post(f"/api/uploads/{uid}/complete")
    assert resp.status_code == 409
    assert resp.json()["code"] == "UPLOAD_INCOMPLETE"


async def test_complete_size_mismatch(client):
    body = await _create(client, size=10)
    uid = body["upload_id"]
    for idx, payload in enumerate([b"abcd", b"efgh", b"XXXX"]):  # 实际 12 != 声明 10
        await client.put(f"/api/uploads/{uid}/chunks/{idx}", content=payload)

    resp = await client.post(f"/api/uploads/{uid}/complete")
    assert resp.status_code == 422
    assert resp.json()["code"] == "UPLOAD_CHECKSUM_MISMATCH"


async def test_complete_sha256_mismatch(client):
    resp = await client.post(
        "/api/uploads",
        json={"filename": "deck.pptx", "size": 4, "sha256": "0" * 64},
    )
    uid = resp.json()["upload_id"]
    await client.put(f"/api/uploads/{uid}/chunks/0", content=b"abcd")

    done = await client.post(f"/api/uploads/{uid}/complete")
    assert done.status_code == 422
    assert done.json()["code"] == "UPLOAD_CHECKSUM_MISMATCH"


async def test_complete_sha256_match(client):
    import hashlib

    payload = b"abcd"
    resp = await client.post(
        "/api/uploads",
        json={
            "filename": "deck.pptx",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    uid = resp.json()["upload_id"]
    await client.put(f"/api/uploads/{uid}/chunks/0", content=payload)

    done = await client.post(f"/api/uploads/{uid}/complete")
    assert done.status_code == 200


async def test_expired_sessions_are_purged_on_create(client):
    from datetime import datetime, timedelta, timezone

    from app.db import SessionLocal
    from app.models import Upload

    first = (
        await client.post("/api/uploads", json={"filename": "old.pptx", "size": 4})
    ).json()
    await client.put(f"/api/uploads/{first['upload_id']}/chunks/0", content=b"abcd")

    # 把会话强制置为已过期
    session = SessionLocal()
    stale = session.get(Upload, first["upload_id"])
    stale.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.commit()
    session.close()

    await client.post("/api/uploads", json={"filename": "new.pptx", "size": 4})

    session = SessionLocal()
    assert session.get(Upload, first["upload_id"]).status == "expired"
    session.close()
    assert not (settings.uploads_dir / first["upload_id"]).exists()


async def test_complete_returns_task_id(client):
    body = await _create(client, size=10)
    uid = body["upload_id"]
    for idx, payload in enumerate([b"abcd", b"efgh", b"ij"]):
        await client.put(f"/api/uploads/{uid}/chunks/{idx}", content=payload)

    resp = await client.post(f"/api/uploads/{uid}/complete")
    assert resp.status_code == 200
    assert resp.json()["task_id"]


async def test_run_task_records_failure_when_first_commit_fails(client, monkeypatch):
    """回归测试：run_task 兜底分支自身的 commit 若失败会让任务永久卡死。

    模拟真实 DB 错误在第一次 _set_status("parsing") 的 commit 上发生：
    run_task 必须靠 _record_failure 的回滚重试自愈，最终把任务落到
    failed 状态并写入 error_code，而不是让异常从 run_task 逃逸、
    把任务永远卡在 pending/parsing。
    """
    from sqlalchemy.orm import Session as OrmSession

    from app.db import SessionLocal
    from app.models import Task
    from app.services import pipeline

    task_id = "task-commit-fail"
    setup = SessionLocal()
    setup.add(
        Task(
            task_id=task_id,
            upload_id="upload-does-not-matter",
            original_filename="deck.pptx",
            size_bytes=4,
            status="pending",
            engine="placeholder",
        )
    )
    setup.commit()
    setup.close()

    original_commit = OrmSession.commit
    calls = {"n": 0}

    def flaky_commit(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated DB commit failure")
        return original_commit(self)

    monkeypatch.setattr(OrmSession, "commit", flaky_commit)

    pipeline.run_task(task_id)  # 不应向外抛异常

    check = SessionLocal()
    persisted = check.get(Task, task_id)
    check.close()
    assert persisted.status == "failed"
    assert persisted.error_code == "INTERNAL_ERROR"
    assert persisted.error_message == "simulated DB commit failure"


async def test_complete_sha256_io_error_returns_storage_full(client, monkeypatch):
    """回归测试：_sha256_of 读取阶段的裸 OSError 必须归一化成 StorageFull。

    真实触发路径包括并发 complete 撞车、AV 软件对刚写完的大文件加短暂锁等，
    都会让 path.open("rb") 抛 OSError 子类，不能绕过全局 AppError 契约。
    """
    import hashlib
    from pathlib import Path

    payload = b"abcd"
    resp = await client.post(
        "/api/uploads",
        json={
            "filename": "deck.pptx",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    uid = resp.json()["upload_id"]
    await client.put(f"/api/uploads/{uid}/chunks/0", content=payload)

    original_open = Path.open

    def flaky_open(self, mode="r", *args, **kwargs):
        # 只拦截对已拼装 .pptx 文件的只读打开（即 _sha256_of 的调用路径），
        # 放行 ChunkStore.assemble 的写入（"wb"）与其它无关文件操作。
        if self.suffix == ".pptx" and mode == "rb":
            raise PermissionError("simulated AV lock")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)

    done = await client.post(f"/api/uploads/{uid}/complete")
    assert done.status_code == 507
    assert done.json()["code"] == "STORAGE_FULL"


async def test_put_chunk_rejects_oversized_content_length_before_reading_body(client):
    """回归测试：块大小校验必须在读 body 之前先看 Content-Length 提前拒绝。

    读后复验（len(data) > chunk_size）仍然保留作为最后防线，但这里验证
    声明的 Content-Length 超限时能直接 413，不依赖是否真的把 body 读完。
    """
    body = await _create(client, size=10)
    uid = body["upload_id"]

    resp = await client.put(
        f"/api/uploads/{uid}/chunks/0",
        content=b"ab",
        headers={"content-length": "999999"},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "UPLOAD_SIZE_EXCEEDED"
