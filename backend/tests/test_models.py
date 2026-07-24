from datetime import datetime, timedelta, timezone

from app.models import Task, Upload


def test_upload_roundtrip(session):
    up = Upload(
        upload_id="u1",
        filename="deck.pptx",
        size_bytes=1024,
        sha256=None,
        chunk_size=5 * 1024 * 1024,
        total_chunks=1,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(up)
    session.commit()

    loaded = session.get(Upload, "u1")
    assert loaded.filename == "deck.pptx"
    assert loaded.status == "active"


def test_task_defaults(session):
    task = Task(
        task_id="t1",
        upload_id="u1",
        original_filename="deck.pptx",
        size_bytes=1024,
    )
    session.add(task)
    session.commit()

    loaded = session.get(Task, "t1")
    assert loaded.status == "pending"
    assert loaded.engine == "placeholder"
    assert loaded.user_id is None
