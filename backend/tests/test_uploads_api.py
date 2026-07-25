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
