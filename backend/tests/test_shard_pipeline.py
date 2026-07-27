"""分片流水线的纯逻辑测试。

不需要 Azure 凭证：引擎一律用假引擎注入，覆盖的是编排本身——页范围规划、
切片后的体积实测与重切、分片数/合并体积上限、状态机转换、合并顺序、
失败传播、中间产物清理。

测试分三层，缺一层就会留下"改一行悄悄退回"的缺口：
1. 子函数层（prepare_shards / convert_shard / merge_shards 各自的行为）；
2. 接线层（run_task 到底有没有走分片分支、有没有把 shard_ids 交给队列）；
3. 队列层（enqueue_shards 到底有没有用 Dependency(allow_failure=True)）。
"""
import io
import random
import shutil
from pathlib import Path

import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Emu, Inches
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

import app.services.pipeline as pipeline_module
import app.services.retention as retention_module
import app.services.shard_pipeline as shard_module
from app.config import settings
from app.errors import ConversionTimeout, ShardBudgetExceeded, ShardTooLarge
from app.models import Task, TaskShard
from app.services.shard_pipeline import (
    convert_shard,
    merge_shards,
    prepare_shards,
    shard_dir,
)

DECK_SLIDES = 8


# ---------------------------------------------------------------- 素材构造


def _pdf(path: Path, labels: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter)
    for label in labels:
        c.setFont("Helvetica", 40)
        c.drawCentredString(300, 400, label)
        c.showPage()
    c.save()
    return path


HEAVY_SLIDES = 2
"""前两页各嵌一张约 1MB 的噪声图，其余页只有标题。

刻意做成极端失真的体积分布——这正是 plan_ranges 把终局判定权交给调用方的
理由。若每页体积均匀，「均摊估算」和「实测」就几乎重合，重切逻辑永远触发
不到，这个文件里关于实测复核的测试也就测不到东西。
"""


def _noisy_png(seed: int, w: int = 700, h: int = 450) -> io.BytesIO:
    """随机噪声图几乎不可压缩，单张约 1MB。

    seed 必须逐页不同：python-pptx 按图片字节做去重，两页塞同一张图会
    合并成同一个 media part，体积分布就又变均匀了。
    """
    rng = random.Random(seed)
    img = Image.new("RGB", (w, h))
    img.putdata(
        [
            (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
            for _ in range(w * h)
        ]
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


@pytest.fixture(scope="session")
def _deck_master(tmp_path_factory) -> Path:
    """构造一次、全模块复用——噪声图生成是这个文件里最慢的一步。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
    for i in range(DECK_SLIDES):
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = f"PAGE-{i + 1}"
        if i < HEAVY_SLIDES:
            s.shapes.add_picture(_noisy_png(seed=i), Inches(1), Inches(2), width=Inches(4))
    path = tmp_path_factory.mktemp("deckmaster") / "deck.pptx"
    prs.save(path)
    return path


@pytest.fixture
def deck(tmp_path, _deck_master) -> Path:
    dst = tmp_path / "deck.pptx"
    shutil.copyfile(_deck_master, dst)
    return dst


@pytest.fixture
def tiny_deck_factory(tmp_path):
    """无图片的小 deck，用于只关心页数的分片转换测试。"""

    def _make(path: Path, pages: int) -> Path:
        prs = Presentation()
        for i in range(pages):
            s = prs.slides.add_slide(prs.slide_layouts[5])
            s.shapes.title.text = f"T{i + 1}"
        path.parent.mkdir(parents=True, exist_ok=True)
        prs.save(path)
        return path

    return _make


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    settings.ensure_dirs()
    return settings.storage_root


class _FakeEngine:
    """假转换引擎：记录每次调用的参数，按需产出 PDF 或抛错。"""

    def __init__(self, pages: int | None = None, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.pages = pages
        self.exc = exc

    def convert(self, src: Path, meta, dest: Path, *, timeout_s: float) -> None:
        self.calls.append(
            {
                "src": Path(src),
                "dest": Path(dest),
                "timeout_s": timeout_s,
                "slide_count": meta.slide_count,
            }
        )
        if self.exc is not None:
            raise self.exc
        n = self.pages if self.pages is not None else meta.slide_count
        _pdf(Path(dest), [f"X{i}" for i in range(n)])


def _install_engine(monkeypatch, engine: _FakeEngine, target=shard_module) -> list[str]:
    """接管 get_engine，并把请求过的引擎名记下来——"有没有偷偷换引擎"
    是本期的红线，必须可断言。"""
    asked: list[str] = []

    def _fake_get_engine(name: str):
        asked.append(name)
        return engine

    monkeypatch.setattr(target, "get_engine", _fake_get_engine)
    return asked


@pytest.fixture
def sharded_task(session, storage):
    task = Task(
        task_id="T1",
        upload_id="U1",
        original_filename="deck.pptx",
        size_bytes=100,
        slide_count=3,
        engine="graph",
        status="converting",
        shard_total=2,
    )
    session.add(task)
    for i, (ps, pe) in enumerate([(1, 2), (3, 3)]):
        session.add(
            TaskShard(
                shard_id=f"S{i}",
                task_id="T1",
                index=i,
                page_start=ps,
                page_end=pe,
                status="pending",
            )
        )
    session.commit()
    return task


@pytest.fixture
def use_test_session(session, monkeypatch):
    monkeypatch.setattr(shard_module, "SessionLocal", lambda: session)
    return session


# ---------------------------------------------------------------- merge_shards


def test_merge_marks_done_when_all_shards_succeed(
    session, sharded_task, use_test_session
):
    d = shard_dir("T1")
    for i, labels in enumerate([["P1", "P2"], ["P3"]]):
        p = _pdf(d / f"{i:03d}.pdf", labels)
        shard = session.get(TaskShard, f"S{i}")
        shard.status = "done"
        shard.output_path = str(p)
    session.commit()

    merge_shards("T1")

    task = session.get(Task, "T1")
    assert task.status == "done"
    assert task.output_path is not None
    assert len(PdfReader(task.output_path).pages) == 3


def test_merge_fails_when_any_shard_failed(session, sharded_task, use_test_session):
    """9 成 1 败也必须整体失败——合并出一份缺了中间几页的 PDF，
    比明确报错糟糕得多。"""
    d = shard_dir("T1")
    p = _pdf(d / "000.pdf", ["P1", "P2"])
    s0 = session.get(TaskShard, "S0")
    s0.status, s0.output_path = "done", str(p)
    s1 = session.get(TaskShard, "S1")
    s1.status, s1.error_code, s1.error_message = "failed", "CONVERSION_TIMEOUT", "转换超时"
    session.commit()

    merge_shards("T1")

    task = session.get(Task, "T1")
    assert task.status == "failed"
    assert task.error_code == "CONVERSION_TIMEOUT"
    assert task.output_path is None


def test_merge_fails_when_shards_unfinished(session, sharded_task, use_test_session):
    """没有失败但也没跑完——汇总 job 被提前触发时不能当成功处理。"""
    d = shard_dir("T1")
    p = _pdf(d / "000.pdf", ["P1", "P2"])
    s0 = session.get(TaskShard, "S0")
    s0.status, s0.output_path = "done", str(p)
    session.commit()  # S1 仍是 pending

    merge_shards("T1")

    task = session.get(Task, "T1")
    assert task.status == "failed"
    assert task.output_path is None


def test_merge_fails_when_page_count_mismatches(session, sharded_task, use_test_session):
    """合并结果的总页数必须等于 slide_count。"""
    d = shard_dir("T1")
    for i, labels in enumerate([["P1"], ["P3"]]):  # 少了一页
        p = _pdf(d / f"{i:03d}.pdf", labels)
        shard = session.get(TaskShard, f"S{i}")
        shard.status, shard.output_path = "done", str(p)
    session.commit()

    merge_shards("T1")

    task = session.get(Task, "T1")
    assert task.status == "failed"
    assert task.error_code == "CONVERSION_PAGE_MISMATCH"
    assert task.output_path is None


def test_merge_uses_index_order_not_filename_order(
    session, sharded_task, use_test_session
):
    """合并顺序只由 TaskShard.index 决定。文件名故意反着排：任何形式的
    按路径排序都会让页序变成 P3,P1,P2。"""
    d = shard_dir("T1")
    p0 = _pdf(d / "zzz.pdf", ["P1", "P2"])
    p1 = _pdf(d / "aaa.pdf", ["P3"])
    for sid, p in (("S0", p0), ("S1", p1)):
        shard = session.get(TaskShard, sid)
        shard.status, shard.output_path = "done", str(p)
    session.commit()

    merge_shards("T1")

    task = session.get(Task, "T1")
    assert task.status == "done"
    texts = [page.extract_text().strip() for page in PdfReader(task.output_path).pages]
    assert texts == ["P1", "P2", "P3"]


def test_merge_rejects_when_total_bytes_exceed_budget(
    session, sharded_task, use_test_session, monkeypatch
):
    """merge_pdfs 会把所有分片一次性载入同一个 PdfWriter（pypdf 没有流式
    合并），峰值约 2.2 倍输入体积。超预算必须在载入之前明确报错，
    而不是让 worker 被 OOM killer 静默干掉、任务永远卡在 running。"""
    d = shard_dir("T1")
    for i, labels in enumerate([["P1", "P2"], ["P3"]]):
        p = _pdf(d / f"{i:03d}.pdf", labels)
        shard = session.get(TaskShard, f"S{i}")
        shard.status, shard.output_path = "done", str(p)
    session.commit()
    monkeypatch.setattr(settings, "graph_max_merge_bytes", 10)

    called: list = []
    monkeypatch.setattr(
        shard_module, "merge_pdfs", lambda *a, **k: called.append(a) or 0
    )

    merge_shards("T1")

    task = session.get(Task, "T1")
    assert task.status == "failed"
    assert task.error_code == "SHARD_BUDGET_EXCEEDED"
    assert called == []  # 关键：根本没进 merge_pdfs


def test_merge_cleans_shard_dir(session, sharded_task, use_test_session):
    d = shard_dir("T1")
    for i, labels in enumerate([["P1", "P2"], ["P3"]]):
        p = _pdf(d / f"{i:03d}.pdf", labels)
        shard = session.get(TaskShard, f"S{i}")
        shard.status, shard.output_path = "done", str(p)
    session.commit()

    merge_shards("T1")
    # 中间产物体积是原文件两倍（分片 pptx + 分片 PDF），必须清掉
    assert not d.exists()


def test_merge_cleans_shard_dir_on_failure(session, sharded_task, use_test_session):
    d = shard_dir("T1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "000.pptx").write_bytes(b"leftover")
    s1 = session.get(TaskShard, "S1")
    s1.status, s1.error_code = "failed", "CONVERSION_FAILED"
    session.commit()

    merge_shards("T1")
    assert not d.exists()


def test_merge_ignores_missing_task(use_test_session):
    merge_shards("does-not-exist")  # 不许抛


# ---------------------------------------------------------------- convert_shard


@pytest.fixture
def one_shard(session, storage, tiny_deck_factory):
    task = Task(
        task_id="T2",
        upload_id="U2",
        original_filename="deck.pptx",
        size_bytes=1000,
        slide_count=4,
        engine="graph",
        status="converting",
        shard_total=1,
    )
    session.add(task)
    session.add(
        TaskShard(
            shard_id="SA",
            task_id="T2",
            index=0,
            page_start=1,
            page_end=2,
            status="pending",
        )
    )
    session.commit()
    tiny_deck_factory(shard_dir("T2") / "000.pptx", 2)
    return task


def test_convert_shard_records_output_and_uses_task_engine(
    session, one_shard, use_test_session, monkeypatch
):
    engine = _FakeEngine()
    asked = _install_engine(monkeypatch, engine)

    convert_shard("SA")

    shard = session.get(TaskShard, "SA")
    assert shard.status == "done"
    assert shard.output_path == str((shard_dir("T2") / "000.pdf").resolve())
    # 绝不静默换引擎：用的必须是 Task.engine
    assert asked == ["graph"]
    call = engine.calls[0]
    assert call["src"] == shard_dir("T2") / "000.pptx"
    assert call["dest"] == shard_dir("T2") / "000.pdf"
    assert call["slide_count"] == 2
    assert call["timeout_s"] > 0
    # 分片 job 不许碰主任务行——并发写同一行在 SQLite 上会丢更新
    assert session.get(Task, "T2").status == "converting"


def test_convert_shard_records_app_error(session, one_shard, use_test_session, monkeypatch):
    _install_engine(monkeypatch, _FakeEngine(exc=ConversionTimeout("超时了")))

    convert_shard("SA")

    shard = session.get(TaskShard, "SA")
    assert shard.status == "failed"
    assert shard.error_code == "CONVERSION_TIMEOUT"
    assert shard.error_message == "超时了"
    assert session.get(Task, "T2").status == "converting"


def test_convert_shard_records_internal_error_on_crash(
    session, one_shard, use_test_session, monkeypatch
):
    """裸异常不能让 RQ 把 job 记成崩溃而分片状态停在 converting——
    那样汇总 job 会看到一个永远不落地的分片。"""
    _install_engine(monkeypatch, _FakeEngine(exc=RuntimeError("boom")))

    convert_shard("SA")

    shard = session.get(TaskShard, "SA")
    assert shard.status == "failed"
    assert shard.error_code == "INTERNAL_ERROR"


def test_convert_shard_fails_when_source_missing(
    session, one_shard, use_test_session, monkeypatch
):
    (shard_dir("T2") / "000.pptx").unlink()
    _install_engine(monkeypatch, _FakeEngine())

    convert_shard("SA")

    shard = session.get(TaskShard, "SA")
    assert shard.status == "failed"
    assert shard.error_code is not None


def test_convert_shard_ignores_missing_shard(use_test_session):
    convert_shard("nope")  # 不许抛


# ---------------------------------------------------------------- prepare_shards


def _make_task(session, deck: Path, task_id: str = "T3") -> Task:
    src = settings.originals_dir / f"{task_id}.pptx"
    shutil.copyfile(deck, src)
    task = Task(
        task_id=task_id,
        upload_id="U3",
        original_filename="deck.pptx",
        size_bytes=src.stat().st_size,
        slide_count=DECK_SLIDES,
        engine="graph",
        requested_engine="graph",
        status="converting",
    )
    session.add(task)
    session.commit()
    return task


def test_prepare_shards_creates_rows_and_files(session, storage, deck, monkeypatch):
    monkeypatch.setattr(settings, "graph_max_pages_per_shard", 2)
    task = _make_task(session, deck)
    src = settings.originals_dir / "T3.pptx"

    shard_ids = prepare_shards(session, task, src, src.stat().st_size)

    assert len(shard_ids) == 4
    assert task.shard_total == 4
    shards = (
        session.query(TaskShard)
        .filter(TaskShard.task_id == "T3")
        .order_by(TaskShard.index)
        .all()
    )
    assert [s.index for s in shards] == [0, 1, 2, 3]
    assert [(s.page_start, s.page_end) for s in shards] == [(1, 2), (3, 4), (5, 6), (7, 8)]
    for s in shards:
        assert (shard_dir("T3") / f"{s.index:03d}.pptx").is_file()
    # 中间的 scratch 目录必须清干净，只留最终分片
    assert sorted(p.name for p in shard_dir("T3").iterdir()) == [
        "000.pptx",
        "001.pptx",
        "002.pptx",
        "003.pptx",
    ]


def test_prepare_shards_resplits_when_measured_size_exceeds_budget(
    session, storage, deck, monkeypatch
):
    """plan_ranges 只有「总体积 ÷ 页数」的均值可用，共享的 theme/master 在
    每片里各留一份，所以实测总是比均摊估算大。调用方必须实测复核并再切一轮，
    否则规划器放行的分片会直接怼给 Graph，换回一个难懂的 Graph 错误。"""
    from app.services.shard_planner import plan_ranges

    task = _make_task(session, deck)
    src = settings.originals_dir / "T3.pptx"
    size = src.stat().st_size
    monkeypatch.setattr(settings, "graph_max_pages_per_shard", DECK_SLIDES)
    # 体积几乎全集中在前两页。预算取全包的 3/4：
    # - 均摊估算看到的每页只有 size/8，于是认为 6 页一片都塞得下 → 规划 2 片；
    # - 但真正装下前两页的那一片实测接近整包体积，必然超预算，只有把两张
    #   重图拆到不同分片才收敛。
    monkeypatch.setattr(settings, "graph_max_shard_bytes", int(size * 0.75))

    planned = plan_ranges(
        DECK_SLIDES, size, settings.graph_max_pages_per_shard, settings.graph_max_shard_bytes
    )
    assert len(planned) == 2  # 估算说 2 片就够

    shard_ids = prepare_shards(session, task, src, size)

    assert len(shard_ids) > len(planned)  # 实测复核之后必须切得更细
    shards = (
        session.query(TaskShard)
        .filter(TaskShard.task_id == "T3")
        .order_by(TaskShard.index)
        .all()
    )
    # 覆盖完整、无缝无重叠
    assert shards[0].page_start == 1
    assert shards[-1].page_end == DECK_SLIDES
    for prev, nxt in zip(shards, shards[1:]):
        assert nxt.page_start == prev.page_end + 1
    # 每一片的实测体积都在预算内——这才是本条契约的终局判定
    for s in shards:
        path = shard_dir("T3") / f"{s.index:03d}.pptx"
        assert path.stat().st_size <= settings.graph_max_shard_bytes


def test_prepare_shards_raises_when_single_page_too_large(
    session, storage, deck, monkeypatch
):
    """单片已经只剩 1 页却仍然超限，没有再切的余地——必须响亮失败，
    不能无限重切，也不能静默放行。"""
    monkeypatch.setattr(settings, "graph_max_pages_per_shard", DECK_SLIDES)
    monkeypatch.setattr(settings, "graph_max_shard_bytes", 1)
    monkeypatch.setattr(settings, "graph_max_shards", 1000)
    task = _make_task(session, deck)
    src = settings.originals_dir / "T3.pptx"

    with pytest.raises(ShardTooLarge):
        prepare_shards(session, task, src, src.stat().st_size)

    assert task.shard_total is None
    assert not shard_dir("T3").exists()  # 失败路径也要清理中间产物


def test_prepare_shards_rejects_too_many_shards_at_planning(
    session, storage, deck, monkeypatch
):
    """分片总数必须有显式上限：merge_pdfs 的峰值内存正比于分片总量。
    规划阶段就已经超限时快速失败，不必先切一整轮 IO 再拒绝。"""
    monkeypatch.setattr(settings, "graph_max_pages_per_shard", 1)
    monkeypatch.setattr(settings, "graph_max_shards", 3)
    task = _make_task(session, deck)
    src = settings.originals_dir / "T3.pptx"

    with pytest.raises(ShardBudgetExceeded):
        prepare_shards(session, task, src, src.stat().st_size)

    assert session.query(TaskShard).filter(TaskShard.task_id == "T3").count() == 0
    assert not shard_dir("T3").exists()


def test_prepare_shards_rejects_when_resplit_exceeds_shard_cap(
    session, storage, deck, monkeypatch
):
    """上限必须在实测复核之后再判一次。重切会让分片数比规划值多，只卡规划
    输出等于没卡——这里规划只要 2 片（远在上限内），重切之后才超。"""
    from app.services.shard_planner import plan_ranges

    task = _make_task(session, deck)
    src = settings.originals_dir / "T3.pptx"
    size = src.stat().st_size
    monkeypatch.setattr(settings, "graph_max_pages_per_shard", DECK_SLIDES)
    monkeypatch.setattr(settings, "graph_max_shard_bytes", int(size * 0.75))
    monkeypatch.setattr(settings, "graph_max_shards", 3)

    assert (
        len(
            plan_ranges(
                DECK_SLIDES,
                size,
                settings.graph_max_pages_per_shard,
                settings.graph_max_shard_bytes,
            )
        )
        <= settings.graph_max_shards
    )  # 规划阶段的快速失败这次不会触发

    with pytest.raises(ShardBudgetExceeded):
        prepare_shards(session, task, src, size)

    assert session.query(TaskShard).filter(TaskShard.task_id == "T3").count() == 0
    assert not shard_dir("T3").exists()


# ---------------------------------------------------------------- run_task 接线


@pytest.fixture
def wired_pipeline(session, storage, monkeypatch):
    """把 run_task 需要的三个外部依赖接到测试会话上，再把入队换成记录器。"""
    monkeypatch.setattr(pipeline_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(retention_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(shard_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        pipeline_module, "select_engine", lambda meta, size_bytes, requested=None: "graph"
    )
    enqueued: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        pipeline_module,
        "enqueue_shards",
        lambda task_id, shard_ids: enqueued.append((task_id, list(shard_ids))),
    )
    return enqueued


def test_run_task_routes_to_sharded_path(session, deck, wired_pipeline, monkeypatch):
    """接线断言：删掉 run_task 里的 prepare_shards 调用，行不会建；
    删掉 enqueue_shards 调用，记录器是空的。两者任一都会让本测试变红。"""
    monkeypatch.setattr(settings, "graph_max_pages_per_shard", 2)
    task = _make_task(session, deck, task_id="T4")
    engine = _FakeEngine()
    asked = _install_engine(monkeypatch, engine, target=pipeline_module)

    pipeline_module.run_task("T4")

    assert task.shard_total == 4
    assert session.query(TaskShard).filter(TaskShard.task_id == "T4").count() == 4
    assert wired_pipeline == [("T4", [s.shard_id for s in _shards(session, "T4")])]
    # 分片路径不走单次转换
    assert engine.calls == []
    assert asked == []
    # 任务留在 converting，等汇总 job 落终态
    assert session.get(Task, "T4").status == "converting"


def test_run_task_keeps_single_conversion_when_not_sharding(
    session, deck, wired_pipeline, monkeypatch
):
    monkeypatch.setattr(settings, "graph_max_pages_per_shard", 100)
    monkeypatch.setattr(settings, "graph_max_shard_bytes", 500 * 1024 * 1024)
    task = _make_task(session, deck, task_id="T5")
    engine = _FakeEngine(pages=DECK_SLIDES)
    _install_engine(monkeypatch, engine, target=pipeline_module)

    pipeline_module.run_task("T5")

    assert wired_pipeline == []
    assert task.shard_total is None
    assert len(engine.calls) == 1
    assert session.get(Task, "T5").status == "done"


def test_run_task_fails_loudly_instead_of_falling_back(
    session, deck, wired_pipeline, monkeypatch
):
    """用户显式选了 Graph 而条件不满足时必须明确报错，
    绝不能偷偷改用 LibreOffice。"""
    monkeypatch.setattr(settings, "graph_max_pages_per_shard", DECK_SLIDES)
    monkeypatch.setattr(settings, "graph_max_shard_bytes", 1)
    task = _make_task(session, deck, task_id="T6")
    engine = _FakeEngine()
    asked = _install_engine(monkeypatch, engine, target=pipeline_module)

    pipeline_module.run_task("T6")

    task = session.get(Task, "T6")
    assert task.status == "failed"
    assert task.error_code == "SHARD_TOO_LARGE"
    assert task.engine == "graph"  # 引擎没有被悄悄换掉
    assert engine.calls == []
    assert asked == []
    assert wired_pipeline == []


def _shards(session, task_id: str) -> list[TaskShard]:
    return (
        session.query(TaskShard)
        .filter(TaskShard.task_id == task_id)
        .order_by(TaskShard.index)
        .all()
    )


# ---------------------------------------------------------------- 队列接线


def test_enqueue_shards_uses_dependency_with_allow_failure(monkeypatch):
    """allow_failure=True 是必须的：默认的 False 会让任一分片失败时汇总 job
    永远停在 DeferredJobRegistry 里不执行，任务卡死在 converting。"""
    from unittest.mock import Mock

    from rq.job import Dependency, Job

    import app.queue as queue_module
    from app.services.shard_pipeline import convert_shard as cs
    from app.services.shard_pipeline import merge_shards as ms

    class _FakeQueue:
        """只记录 enqueue 调用，不碰 Redis。返回真 Job 实例——Dependency
        会拒绝非 Job/str 的对象，用假对象测不到真实约束。"""

        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def enqueue(self, func, *args, **kwargs):
            job = Job(id=f"job-{len(self.calls)}", connection=Mock())
            self.calls.append((func, args, kwargs, job))
            return job

    q = _FakeQueue()
    monkeypatch.setattr(queue_module, "get_queue", lambda: q)

    queue_module.enqueue_shards("T9", ["a", "b", "c"])

    assert len(q.calls) == 4
    for i, sid in enumerate(["a", "b", "c"]):
        func, args, kwargs, _job = q.calls[i]
        assert func is cs
        assert args == (sid,)
        assert kwargs["job_timeout"] > 0

    func, args, kwargs, _job = q.calls[3]
    assert func is ms
    assert args == ("T9",)
    dep = kwargs["depends_on"]
    assert isinstance(dep, Dependency)
    assert dep.allow_failure is True
    assert dep.dependencies == [c[3] for c in q.calls[:3]]
