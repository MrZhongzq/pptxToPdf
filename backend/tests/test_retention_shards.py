"""分片目录的保留策略：purge_expired_shards() 与它在 run_task 里的接线。

正常路径下 merge_shards 的 finally 会自己 rmtree 分片目录（见
test_shard_pipeline.py::test_merge_cleans_shard_dir*）；discard_shards 处理
入队失败的撤销路径。这个文件测的是两者都不会碰的第三条路径——worker 在
convert_shard / merge_shards 中途被 OOM killer 杀掉，没有任何 finally 会跑，
分片目录（几十 MB 到几百 MB）就成了永久孤儿，只能靠这个惰性清理兜底。
"""
import os
import time

import pytest

from app.config import settings
from app.services.retention import purge_expired_shards


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    settings.ensure_dirs()
    return tmp_path


def test_purges_expired_shard_dirs(storage, monkeypatch):
    monkeypatch.setattr(settings, "output_ttl_hours", 24)
    old = settings.shards_dir / "old-task"
    old.mkdir(parents=True)
    (old / "000.pptx").write_bytes(b"x")
    past = time.time() - 25 * 3600
    os.utime(old, (past, past))

    fresh = settings.shards_dir / "fresh-task"
    fresh.mkdir(parents=True)
    (fresh / "000.pptx").write_bytes(b"x")

    assert purge_expired_shards() == 1
    assert not old.exists()
    assert fresh.exists()


def test_missing_dir_is_not_an_error(storage):
    import shutil

    shutil.rmtree(settings.shards_dir)
    assert purge_expired_shards() == 0


def test_purge_expired_shards_is_called_from_run_task_finally(
    storage, session, monkeypatch
):
    """线索：把它接进 run_task 的 finally，不能只是定义了没人调用。

    刻意不构造真实分片场景——finally 无论走成功还是失败分支都必须触发，
    所以用一个连源文件都不存在的任务（probe 会炸成 INTERNAL_ERROR）来验证
    这条接线跟转换本身成败无关，纯粹是「函数被调用过」的接线断言。
    """
    import app.services.pipeline as pipeline_module
    import app.services.retention as retention_module
    from app.models import Task

    monkeypatch.setattr(pipeline_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(retention_module, "SessionLocal", lambda: session)

    calls: list[None] = []
    monkeypatch.setattr(
        pipeline_module,
        "purge_expired_shards",
        lambda: calls.append(None) or 0,
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
