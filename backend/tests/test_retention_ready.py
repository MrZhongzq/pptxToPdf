"""ready 任务的 1 小时 TTL 回收：purge_expired_ready() 与其它清理路径的边界。

Task 4 把 complete 之后的任务落成 ready、不入队，等用户点「开始转换」。
这就带来一个新的存储风险：用户传完大文件却不点按钮，原文件永久占盘。
本文件测的是给 ready 状态单独加的一条清理路径，以及它跟另外两条既有
路径的边界：
  - 孤儿回收器（reap_stale_tasks，45 分钟）管的是「转换卡住了」，
    ready 只是「在等人点按钮」，两者不能互相踩线；
  - 未完成的上传会话（Upload.status == "active"）归 upload TTL（24 小时，
    支持断点续传）管，ready TTL 更短、更激进，不能误伤它。
"""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

import app.services.retention as retention_module
from app.config import settings
from app.models import Task, Upload
from app.services.retention import purge_expired_ready, reap_stale_tasks


@pytest.fixture
def use_retention_session(session, monkeypatch):
    """把 retention.py 模块级的 SessionLocal 重定向到本用例的 session fixture。

    conftest.py 的 autouse _isolate_app_db 只重定向到它自己建的
    tmp_path/'isolated.db'，跟 `session` fixture 建的 tmp_path/'test.db'
    是两个不同的 sqlite 文件——不加这层，purge_expired_ready 连的库里
    永远没有本文件造的数据，所有断言都会「因为库是空的」而不是因为逻辑
    正确而通过。做法照抄 test_shard_pipeline.py:432 已验证过的模式。
    """
    monkeypatch.setattr(retention_module, "SessionLocal", lambda: session)
    return session


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    settings.ensure_dirs()
    return tmp_path


def _stale(minutes: int) -> datetime:
    """naive UTC——SQLite dialect 落库时会丢时区，retention 也用 naive 比较。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes)


def _ready_task(session, task_id: str, *, minutes_old: int) -> Task:
    task = Task(
        task_id=task_id,
        upload_id=f"U-{task_id}",
        original_filename="deck.pptx",
        size_bytes=100,
        status="ready",
        engine="unassigned",
        updated_at=_stale(minutes_old),
    )
    session.add(task)
    session.commit()
    return task


def test_fresh_ready_task_is_kept(session, use_retention_session, monkeypatch):
    """59 分钟的 ready 任务不该被回收。

    TTL monkeypatch 成 2（不是默认值 1）：如果拿 1.5 小时的任务去测默认
    1 小时 TTL，函数不管有没有正确接住 settings.ready_ttl_hours 都会
    「保留」，测不出接错配置项的问题。
    """
    monkeypatch.setattr(settings, "ready_ttl_hours", 2)
    task_id = "T-fresh"
    _ready_task(session, task_id, minutes_old=90)  # 1.5h < 2h TTL

    assert purge_expired_ready() == 0

    task = session.get(Task, task_id)
    assert task.status == "ready"


def test_expired_ready_task_is_reaped(session, use_retention_session, monkeypatch, storage):
    """超过 TTL 的 ready 任务：原文件删掉、Task 落 failed + READY_EXPIRED。"""
    monkeypatch.setattr(settings, "ready_ttl_hours", 2)
    task_id = "T-expired"
    original = settings.originals_dir / f"{task_id}.pptx"
    original.write_bytes(b"fake-pptx-bytes")
    _ready_task(session, task_id, minutes_old=150)  # 2.5h > 2h TTL

    assert purge_expired_ready() == 1

    task = session.get(Task, task_id)
    assert task.status == "failed"
    assert task.error_code == "READY_EXPIRED"
    assert not original.exists()


def test_ready_is_not_touched_by_stale_reaper(session, use_retention_session):
    """孤儿回收器（45 分钟）管的是「转换卡住」，ready 只是在等人点按钮。

    造一个 46 分钟没动的 ready 任务，reap_stale_tasks 应返回 0——不
    monkeypatch stale_task_minutes，就用默认 45，46 分钟正好卡在阈值上方，
    如果 NON_TERMINAL 里混进了 "ready" 会立刻穿帮。
    """
    task_id = "T-orphan-looking"
    _ready_task(session, task_id, minutes_old=46)

    assert reap_stale_tasks() == 0

    task = session.get(Task, task_id)
    assert task.status == "ready"
    assert task.error_code is None


def test_ready_ttl_does_not_touch_unfinished_uploads(
    session, use_retention_session, storage, monkeypatch
):
    """ready TTL 不许误伤未完成的上传会话——那归 upload TTL（24 小时）管，
    调短它会让大文件传到一半、暂停超时后必须从头重传。

    造一个真实、磁盘上有分片文件的 active 上传会话，并把分片文件的 mtime
    调到 2 小时前——如果 purge_expired_ready 被误写成像 purge_expired_shards
    那样按 mtime 扫 uploads_dir，这里就会被误删；只在 DB 里插一行 Upload
    而不落盘文件的话，这条测试测不出这种误写，没有区分力。同时造一个真
    过期的 ready 任务，确保 purge_expired_ready 这次调用确实执行了删除
    动作，而不是因为压根没有过期任务、函数空转就侥幸通过断言。
    """
    monkeypatch.setattr(settings, "ready_ttl_hours", 2)

    upload_id = "UP-active"
    chunk_dir = settings.uploads_dir / upload_id
    chunk_dir.mkdir(parents=True)
    chunk_path = chunk_dir / "000000.part"
    chunk_path.write_bytes(b"partial-chunk-bytes")
    old = time.time() - 2 * 3600
    os.utime(chunk_path, (old, old))
    os.utime(chunk_dir, (old, old))

    session.add(
        Upload(
            upload_id=upload_id,
            filename="big-deck.pptx",
            size_bytes=500 * 1024 * 1024,
            chunk_size=5 * 1024 * 1024,
            total_chunks=100,
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=22),
        )
    )

    task_id = "T-expired-alongside"
    original = settings.originals_dir / f"{task_id}.pptx"
    original.write_bytes(b"fake-pptx-bytes")
    _ready_task(session, task_id, minutes_old=150)

    assert purge_expired_ready() == 1

    assert chunk_path.exists()
    assert chunk_dir.exists()


def test_non_ready_tasks_are_not_touched(session, use_retention_session):
    """converting / done / failed 的任务不受 ready TTL 影响。"""
    for status in ("converting", "done", "failed"):
        session.add(
            Task(
                task_id=f"T-{status}",
                upload_id=f"U-{status}",
                original_filename="deck.pptx",
                size_bytes=100,
                status=status,
                engine="placeholder",
                updated_at=_stale(200),
            )
        )
    session.commit()

    assert purge_expired_ready() == 0

    for status in ("converting", "done", "failed"):
        task = session.get(Task, f"T-{status}")
        assert task.status == status


# ---------------------------------------------------- 接线守护（终审 I-6）


def test_purge_expired_ready_is_called_from_run_task_finally(session, storage, monkeypatch):
    """线索：把它接进 run_task 的 finally，不能只是定义了没人调用。

    终审同时删掉 pipeline.py 与 main.py 里的调用点做过变异实验，318 条测试
    全绿——spec 自己列的"接线守护"清单漏了这一条。做法照抄
    test_retention_shards.py::test_purge_expired_shards_is_called_from_run_task_finally
    的既有模式：刻意用一个连源文件都不存在的任务（probe 会炸成
    INTERNAL_ERROR），证明这条接线跟转换本身成败无关，纯粹是"函数被调用
    过"的断言。
    """
    import app.services.pipeline as pipeline_module
    from app.models import Task

    monkeypatch.setattr(pipeline_module, "SessionLocal", lambda: session)

    calls: list[None] = []
    monkeypatch.setattr(
        pipeline_module, "purge_expired_ready", lambda: calls.append(None) or 0
    )

    task_id = "task-no-src-file"
    session.add(
        Task(
            task_id=task_id,
            upload_id="upload-does-not-matter",
            original_filename="deck.pptx",
            size_bytes=1,
            status="pending",
            engine="placeholder",
        )
    )
    session.commit()

    pipeline_module.run_task(task_id)

    assert calls == [None]


def test_startup_hook_also_purges_expired_ready(storage, monkeypatch):
    """只挂 run_task 的 finally 不够——长期没有新任务进来时磁盘也没在涨，
    但那正是 ready TTL 存在的理由（用户只传不点开始，恰恰不会有任何
    转换跑完，run_task 的 finally 永远不会触发）。main.py 的启动钩子是
    另一半兜底，照抄 test_retention_shards.py::
    test_startup_hook_also_purges_expired_shards 的既有模式。
    """
    import app.main as main_module

    calls: list[None] = []
    monkeypatch.setattr(
        main_module, "purge_expired_ready", lambda: calls.append(None) or 0
    )

    main_module.startup()

    assert calls == [None]
