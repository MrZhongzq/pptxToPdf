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
    # 24（默认值）对这条断言没有区分力：不 monkeypatch 也是 24，测不出
    # purge_expired_shards 到底读没读 settings.output_ttl_hours——比如把它
    # 误接成 settings.upload_ttl_hours（同样默认 24）也会全绿。这里换成
    # 非默认值 1，时间偏移跟着改成 2 小时，读错配置项就会立刻穿帮。
    monkeypatch.setattr(settings, "output_ttl_hours", 1)
    old = settings.shards_dir / "old-task"
    old.mkdir(parents=True)
    (old / "000.pptx").write_bytes(b"x")
    past = time.time() - 2 * 3600
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


def test_scan_failure_other_than_missing_dir_logs_warning(storage, caplog):
    """M2：目录不存在是正常路径（静默返回 0），但权限错误/IO 错误这类其它
    OSError 性质不同——不吭声的话清理会永久静默失效，磁盘持续增长却没有
    任何日志信号。用同名文件顶替目录，制造一个真实可复现的 NotADirectoryError
    （OSError 子类，但不是 FileNotFoundError），而不是打桩内部实现。"""
    import shutil

    shutil.rmtree(settings.shards_dir)
    settings.shards_dir.write_bytes(b"not a directory")

    with caplog.at_level("WARNING"):
        assert purge_expired_shards() == 0

    assert any("扫描分片目录失败" in r.message for r in caplog.records)


def test_failed_rmtree_is_not_counted_as_removed(storage, monkeypatch, caplog):
    """M3：rmtree(ignore_errors=True) 失败不抛异常，但不能被计入删除数——
    否则 pipeline.py 里那句 info 日志会虚报「清理了 N 个」，磁盘其实一个
    没瘦下来，运维会误判清理生效。这里用一个吞掉调用但什么都不做的假
    rmtree 模拟「Windows 上文件被占用，ignore_errors 悄悄吞掉失败」的场景：
    目录纹丝不动，函数必须自己发现并且不计数。"""
    import app.services.retention as retention_module

    monkeypatch.setattr(settings, "output_ttl_hours", 1)
    old = settings.shards_dir / "stuck-task"
    old.mkdir(parents=True)
    (old / "000.pptx").write_bytes(b"x")
    past = time.time() - 2 * 3600
    os.utime(old, (past, past))

    monkeypatch.setattr(retention_module.shutil, "rmtree", lambda *a, **k: None)

    with caplog.at_level("WARNING"):
        assert purge_expired_shards() == 0

    assert old.exists()
    assert any("仍残留" in r.message for r in caplog.records)


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


def test_startup_hook_also_purges_expired_shards(storage, monkeypatch):
    """M5：只挂 run_task 的 finally 不够——OOM 之后最典型的运维动作就是
    重启，如果重启后长期没有新任务进来，惰性清理永远不跑，残骸永久占盘。
    reap_stale_tasks 因为同一理由做了「启动 + 惰性」双触发（main.py 与
    pipeline.py 对称调用），这里验证 purge_expired_shards 补齐了另一半。
    """
    import app.main as main_module

    # init_db()/reap_stale_tasks() 会真的碰 DB；autouse 的 _isolate_app_db
    # 已经把 app.db.engine 重定向到本测试的隔离库，这里不需要额外接线。
    calls: list[None] = []
    monkeypatch.setattr(
        main_module, "purge_expired_shards", lambda: calls.append(None) or 0
    )

    main_module.startup()

    assert calls == [None]
