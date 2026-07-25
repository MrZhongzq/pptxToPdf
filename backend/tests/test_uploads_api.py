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


def test_record_failure_never_raises_when_commit_fails(client, monkeypatch):
    """单元测试：_record_failure 是本次修复的核心，落库失败必须被吞掉、绝不上抛。

    修复前的实现里根本没有这个函数——两个 except 分支直接操作 task 对象
    再调 _set_status()，其内部 commit() 一旦抛错就会直接从 run_task 逃逸。
    这里直接调用 _record_failure 并让它的 commit 无条件失败，验证它自己
    捕获异常、只记日志，不向调用方传播。
    """
    from app.db import SessionLocal
    from app.models import Task
    from app.services import pipeline

    task_id = "task-record-failure-unit"
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

    session = SessionLocal()

    def boom(self):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(type(session), "commit", boom)

    # 唯一的断言点：这一行不应抛出任何异常。
    pipeline._record_failure(session, task_id, "SOME_CODE", "some message")

    session.close()


def test_run_task_does_not_raise_when_failure_path_commit_also_fails(
    client, monkeypatch
):
    """端到端回归：即使"记录失败状态"这次 commit 本身也失败，run_task 也不能
    把异常抛给 BackgroundTasks 执行器——否则任务会永远卡在中间状态（如
    parsing），error_code 永远是 None，前端无限轮询一个不会再变化的状态。

    构造一个会让 probe() 失败的原始文件（非法 zip），让 run_task 走进
    except 分支；只放行第一次 commit（run_task 里把状态设为 "parsing"
    的那次），之后所有 commit——包括 _record_failure 内部落库失败详情
    的那次——一律抛错，模拟数据库在记录失败详情这一步本身也故障的场景。
    """
    from sqlalchemy.orm import Session as OrmSession

    from app.config import settings
    from app.db import SessionLocal
    from app.models import Task
    from app.services import pipeline

    task_id = "task-failure-path-commit-fails"
    payload = b"not a zip"  # probe() 会因此抛 PptxInvalidZip
    src = settings.originals_dir / f"{task_id}.pptx"
    src.write_bytes(payload)

    setup = SessionLocal()
    setup.add(
        Task(
            task_id=task_id,
            upload_id="upload-does-not-matter",
            original_filename="deck.pptx",
            size_bytes=len(payload),
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
            # 放行 run_task 里把状态设为 "parsing" 的第一次 commit,
            # 这样 probe() 才会真正被调用到并因非法 zip 而抛错。
            return original_commit(self)
        raise RuntimeError("simulated commit failure in failure-recording path")

    monkeypatch.setattr(OrmSession, "commit", flaky_commit)

    # 唯一的断言点：这一行不应抛出任何异常。
    pipeline.run_task(task_id)

    # 确认确实触达了 except 分支里记录失败状态的那次 commit（否则上面的
    # 断言就是在测一个从未进入 except 分支的空转路径）。
    assert calls["n"] >= 2


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
