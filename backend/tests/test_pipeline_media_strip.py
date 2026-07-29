"""五期 Task 3：转换前剥离内嵌媒体，剥离结果必须真的取代原件。

真机那份 83.7MB / 59 页课件，第 25 页嵌 56MB 视频，报 SHARD_TOO_LARGE 且
单页无法再切分；剥离后掉到约 28MB。如果 size_bytes 或切片判定看的还是
剥离前的值，这份课件依旧会被推进切片路径——剥离就白做了。三条测试钉住
这条链路上的三个环节：

1. 剥离发生在 probe 之前；
2. task.size_bytes 记的是剥离后的体积，不是上传时的原始体积；
3. 切片判定（needs_sharding）吃的也是剥离后的值——一个原始体积超阈值、
   剥离后落回阈值内的 deck 不该走切片路径。

参照 tests/test_pipeline_sharding.py 与 tests/test_shard_pipeline.py 里
建 Task、注入假引擎、接管 SessionLocal 的既有写法。
"""
from pathlib import Path

import pytest

import app.services.pipeline as pipeline
from app.config import settings
from app.models import Task
from app.services.media_strip import StripResult
from app.services.pptx_probe import PptxMeta


def _meta(pages: int = 3) -> PptxMeta:
    return PptxMeta(
        slide_count=pages,
        slide_width_emu=12192000,
        slide_height_emu=6858000,
        fonts=(),
    )


@pytest.fixture
def wired_session(session, tmp_path, monkeypatch):
    """接管存储路径与 SessionLocal，让 run_task 用测试自己的会话与目录。

    不接管就会踩两个坑：run_task 默认连到 conftest 的 _isolate_app_db 另开
    的隔离库，看不到这里用 `session` 建的 Task 行（run_task 会在
    `session.get(Task, task_id) is None` 上直接静默返回，测试断言个寂寞）；
    storage_root 不重定向则 originals_dir 落在仓库默认路径下。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    settings.ensure_dirs()
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: session)
    return session


def _make_task(session, task_id: str, content: bytes) -> Task:
    """落一份"剥离前"的原始文件到 originals_dir，size_bytes 也故意写成
    上传时的原始体积——三条测试都要验证 run_task 跑完之后这个值有没有被
    剥离后的体积覆盖。"""
    src = settings.originals_dir / f"{task_id}.pptx"
    src.write_bytes(content)
    task = Task(
        task_id=task_id,
        upload_id=f"U-{task_id}",
        original_filename="deck.pptx",
        size_bytes=len(content),
        status="pending",
    )
    session.add(task)
    session.commit()
    return task


class _FakeEngine:
    """假引擎：只记录被传入的参数，产出一个占位输出文件。

    这三条测试关心的是剥离结果有没有取代原件，不关心真实转换质量——用假
    引擎避开 reportlab 渲染细节与真实 Graph 凭证。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def convert(self, src: Path, meta, dest: Path, *, timeout_s: float) -> None:
        self.calls.append({"src": Path(src), "slide_count": meta.slide_count})
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 fake")


def _install_engine(monkeypatch, engine: _FakeEngine) -> list[str]:
    asked: list[str] = []

    def _fake_get_engine(name: str, **_kwargs):
        asked.append(name)
        return engine

    monkeypatch.setattr(pipeline, "get_engine", _fake_get_engine)
    return asked


# --------------------------------------------------------- 1. 顺序：剥离先于 probe


def test_run_task_strips_media_before_probe(wired_session, monkeypatch):
    """剥离必须发生在 probe 之前——size_bytes 与切片判定都要看剥离后的值。"""
    session = wired_session
    _make_task(session, "T1", b"x" * 200)
    calls: list[tuple[str, int]] = []

    def fake_strip(path: Path) -> StripResult:
        before = path.stat().st_size
        calls.append(("strip", before))
        # 模拟剥掉一半
        path.write_bytes(path.read_bytes()[: before // 2])
        return StripResult(True, 1, before, path.stat().st_size)

    def fake_probe(path: Path) -> PptxMeta:
        calls.append(("probe", path.stat().st_size))
        return _meta()

    monkeypatch.setattr(pipeline, "strip_media", fake_strip)
    monkeypatch.setattr(pipeline, "probe", fake_probe)
    _install_engine(monkeypatch, _FakeEngine())

    pipeline.run_task("T1")

    assert [c[0] for c in calls] == ["strip", "probe"], "剥离必须在 probe 之前"
    assert calls[1][1] < calls[0][1], "probe 看到的应是剥离后的文件"
    assert session.get(Task, "T1").status == "done"


# --------------------------------------------------------- 2. size_bytes 取代原始体积


def test_size_bytes_reflects_stripped_file(wired_session, monkeypatch):
    """task.size_bytes 记的是剥离后的体积，不是用户上传的原始体积。"""
    session = wired_session
    _make_task(session, "T2", b"y" * 300)

    def fake_strip(path: Path) -> StripResult:
        before = path.stat().st_size
        path.write_bytes(path.read_bytes()[:100])
        return StripResult(True, 1, before, path.stat().st_size)

    monkeypatch.setattr(pipeline, "strip_media", fake_strip)
    monkeypatch.setattr(pipeline, "probe", lambda path: _meta())
    _install_engine(monkeypatch, _FakeEngine())

    pipeline.run_task("T2")

    task = session.get(Task, "T2")
    assert task.size_bytes == 100
    assert task.size_bytes != 300  # 不是上传时的原始体积


# --------------------------------------------------------- 3. 切片判定取代原始体积


def test_sharding_decision_uses_stripped_size(wired_session, monkeypatch):
    """一个原始体积超阈值、剥离后落回阈值内的 deck 不该走切片路径。

    真实阈值 graph_max_shard_bytes 默认 40MiB；这里调小到 100 字节以避免
    测试里真写几十 MB 文件，制造同样形状的场景——原始体积超阈值、剥离后
    落回阈值内。这正是真机那份 83.7MB 课件（剥离后约 28MB，落回 40MiB
    阈值内）要走的路。
    """
    session = wired_session
    monkeypatch.setattr(settings, "graph_max_shard_bytes", 100)
    monkeypatch.setattr(settings, "graph_max_pages_per_shard", 80)
    monkeypatch.setattr(
        pipeline,
        "select_engine",
        lambda meta, size_bytes, requested=None, graph_configured=False: "graph",
    )
    monkeypatch.setattr(pipeline, "probe", lambda path: _meta())
    _make_task(session, "T3", b"z" * 200)  # 原始体积 200 > 100 阈值

    def fake_strip(path: Path) -> StripResult:
        before = path.stat().st_size
        path.write_bytes(path.read_bytes()[:50])  # 剥离后 50 < 100 阈值
        return StripResult(True, 1, before, path.stat().st_size)

    monkeypatch.setattr(pipeline, "strip_media", fake_strip)
    engine = _FakeEngine()
    _install_engine(monkeypatch, engine)
    enqueued: list[str] = []
    monkeypatch.setattr(
        pipeline, "enqueue_shards", lambda task_id, shard_ids: enqueued.append(task_id)
    )

    pipeline.run_task("T3")

    task = session.get(Task, "T3")
    assert enqueued == []  # 没有走切片路径
    assert len(engine.calls) == 1  # 走的是单次转换
    assert task.shard_total is None
    assert task.status == "done"
