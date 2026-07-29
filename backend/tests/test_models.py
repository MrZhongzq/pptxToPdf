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
    assert loaded.engine == "unassigned"
    assert loaded.user_id is None


def test_task_shard_defaults(session):
    from app.models import TaskShard

    task = Task(
        task_id="t-shard",
        upload_id="u1",
        original_filename="deck.pptx",
        size_bytes=1024,
        shard_total=3,
    )
    session.add(task)
    shard = TaskShard(
        shard_id="s1",
        task_id="t-shard",
        index=0,
        page_start=1,
        page_end=40,
    )
    session.add(shard)
    session.commit()

    loaded = session.get(TaskShard, "s1")
    assert loaded.status == "pending"
    assert loaded.output_path is None
    assert loaded.error_code is None
    assert session.get(Task, "t-shard").shard_total == 3


def test_task_shard_total_defaults_none(session):
    task = Task(
        task_id="t-plain",
        upload_id="u1",
        original_filename="deck.pptx",
        size_bytes=1024,
    )
    session.add(task)
    session.commit()
    # 不切片的任务 shard_total 必须是 None，而不是 0——
    # 0 会让「未切片」和「切了 0 片」无法区分。
    assert session.get(Task, "t-plain").shard_total is None
