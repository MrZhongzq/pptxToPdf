import hashlib
import os
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.errors import ConversionFailed
from app.services.pdf_merge import merge_pdfs


def _make_pdf(path, labels):
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for label in labels:
        pdf.setFont("Helvetica", 40)
        pdf.drawCentredString(300, 400, label)
        pdf.showPage()
    pdf.save()
    return path


def _texts(path):
    return [(p.extract_text() or "").strip() for p in PdfReader(str(path)).pages]


def test_merge_preserves_order(tmp_path):
    """页序是三期最危险的失败模式：顺序错了页数完全正确，任何页数
    校验都抓不到，用户可能翻到考前才发现第 30 页跑到了第 10 页。"""
    a = _make_pdf(tmp_path / "a.pdf", ["P1", "P2", "P3"])
    b = _make_pdf(tmp_path / "b.pdf", ["P4", "P5"])
    c = _make_pdf(tmp_path / "c.pdf", ["P6"])

    dest = tmp_path / "merged.pdf"
    pages = merge_pdfs([a, b, c], dest)

    assert pages == 6
    assert _texts(dest) == ["P1", "P2", "P3", "P4", "P5", "P6"]


def test_merge_respects_given_order_not_filename(tmp_path):
    """合并顺序必须由入参决定，不能依赖文件名排序——分片文件名是
    000/001/002，一旦有人改成按 glob 排序，10 片时会变成 0,1,10,2…"""
    a = _make_pdf(tmp_path / "z_first.pdf", ["P1"])
    b = _make_pdf(tmp_path / "a_second.pdf", ["P2"])
    dest = tmp_path / "merged.pdf"

    merge_pdfs([a, b], dest)
    assert _texts(dest) == ["P1", "P2"]


def test_merge_ten_shards_numeric_filename_trap(tmp_path):
    """字典序陷阱的直接复现：shard10.pdf 在字典序下排在 shard2.pdf 前面。
    传入顺序是 shard0..shard10 共 11 片，若实现依赖文件名排序而非入参
    顺序，页序会被打乱成 0,1,10,2,3...——本用例逐页断言标签能抓住它。"""
    paths = []
    labels = []
    for i in range(11):
        label = f"P{i}"
        labels.append(label)
        paths.append(_make_pdf(tmp_path / f"shard{i}.pdf", [label]))

    dest = tmp_path / "merged.pdf"
    pages = merge_pdfs(paths, dest)

    assert pages == 11
    assert _texts(dest) == labels


def test_merge_single_part(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["ONLY"])
    dest = tmp_path / "merged.pdf"
    assert merge_pdfs([a], dest) == 1
    assert _texts(dest) == ["ONLY"]


def test_missing_part_raises(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    with pytest.raises(ConversionFailed) as exc:
        merge_pdfs([a, tmp_path / "nope.pdf"], tmp_path / "merged.pdf")
    assert exc.value.code == "CONVERSION_FAILED"


def test_corrupt_part_raises(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(ConversionFailed):
        merge_pdfs([a, bad], tmp_path / "merged.pdf")


def test_empty_parts_raises(tmp_path):
    with pytest.raises(ConversionFailed):
        merge_pdfs([], tmp_path / "merged.pdf")


def test_failed_merge_leaves_no_partial_output(tmp_path):
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    dest = tmp_path / "merged.pdf"
    with pytest.raises(ConversionFailed):
        merge_pdfs([a, bad], dest)
    # 半个合并结果比没有更糟——后续的页数校验会拿它当有效产物
    assert not dest.exists()


# --- 审查后补测：C1/I1/I2/I3，均为审查员实测构造出的输入，不改动上面任何断言 ---


def test_zero_page_shard_raises(tmp_path):
    """C1：合法但 0 页的 PDF 必须报错，不能被静默吞掉。分片对应原 deck
    的一段非空闭区间页范围，贡献 0 页恒为语义错误——这种残缺比页序错乱
    更隐蔽：文件能打开、能预览、页序看着连贯，只是缺了中间一段，
    翻两页看不出来，得对照原稿页数才能发现。"""
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    empty = tmp_path / "empty.pdf"
    canvas.Canvas(str(empty), pagesize=letter).save()  # 合法 PDF，0 页
    b = _make_pdf(tmp_path / "b.pdf", ["P2"])
    dest = tmp_path / "merged.pdf"

    with pytest.raises(ConversionFailed) as exc:
        merge_pdfs([a, empty, b], dest)
    assert not dest.exists()
    # 防回归：0 页检查必须停在"独立于读取异常处理"的位置。如果被挪回
    # 读取阶段的 try 块里，会被 `except Exception` 二次包装——实测这种
    # 二次包装会把两句话拼在一起（"分片 x 无法解析: 分片 x 是 0 页…"），
    # 所以只断言含"0 页"不够（二次包装后仍然含"0 页"），必须同时断言
    # 不含"无法解析"，才能真正抓住"被读取异常处理二次接管"这个回归。
    assert "0 页" in str(exc.value)
    assert "无法解析" not in str(exc.value)


def test_write_phase_oserror_wrapped_as_conversion_failed(tmp_path):
    """I1：写阶段（mkdir/写文件）的 OSError 必须包装成 ConversionFailed，
    不能原样逃逸——brief 的接口契约是 Consumes: ConversionFailed，调用方
    自然会写 except ConversionFailed，裸 OSError 会绕过所有 AppError 分支
    落到通用 500。用「dest 的父路径段已被一个同名文件占用」触发真实的
    mkdir OSError（FileExistsError 是 OSError 子类），不用权限技巧。"""
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    blocked = tmp_path / "blocked"
    blocked.write_bytes(b"i am a file, not a directory")
    dest = blocked / "merged.pdf"  # 父路径段是文件，mkdir(parents=True) 必炸

    with pytest.raises(ConversionFailed):
        merge_pdfs([a], dest)


def test_write_phase_failure_leaves_no_partial_output(tmp_path, monkeypatch):
    """I2：失败清理逻辑此前零覆盖——原有的
    test_failed_merge_leaves_no_partial_output 触发的是读取阶段失败，
    那时从未写过 dest，断言「不存在」有没有清理都成立，没测到清理本身。
    这里 monkeypatch PdfWriter.write 让它先写字节再抛错，真正走一遍
    「写到一半崩」的清理路径。"""
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    dest = tmp_path / "merged.pdf"

    def boom_write(self, fh):
        fh.write(b"%PDF-1.4\npartial garbage")
        raise OSError("simulated disk full mid-write")

    monkeypatch.setattr(PdfWriter, "write", boom_write)

    with pytest.raises(ConversionFailed):
        merge_pdfs([a], dest)

    assert not dest.exists()
    # tmp 文件也不能留下——清理只清自己的 tmp，但必须真的清掉
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_merge_preserves_preexisting_dest(tmp_path):
    """I3：dest 位置已有一份上次成功合并的结果，本次因分片损坏而失败——
    失败发生在读取阶段，一个字节都没写过 dest，旧文件必须原封不动地留着。
    重试语义下把旧结果删掉是数据销毁：从「有结果 + 一次失败」退化成
    「什么都没有」。"""
    old = _make_pdf(tmp_path / "old_result_source.pdf", ["OLD"])
    dest = tmp_path / "merged.pdf"
    dest.write_bytes(old.read_bytes())  # 模拟 dest 位置已有一份好结果

    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")

    with pytest.raises(ConversionFailed):
        merge_pdfs([a, bad], dest)

    assert dest.exists()
    assert _texts(dest) == ["OLD"]


def test_cleanup_failure_does_not_mask_original_error(tmp_path, monkeypatch):
    """I1 附带一层：清理 tmp 文件本身失败时，不能顶掉原始异常。这里让
    os.replace 先失败（模拟写阶段的真实失败），再让 Path.unlink 也失败
    （模拟清理动作本身出问题），断言最终冒出来的仍是包了原始 replace
    失败信息的 ConversionFailed，而不是 unlink 的 PermissionError。"""
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    dest = tmp_path / "merged.pdf"

    def boom_replace(src, dst):
        raise OSError("simulated replace failure")

    def boom_unlink(self, *args, **kwargs):
        raise PermissionError("simulated cleanup failure")

    monkeypatch.setattr(os, "replace", boom_replace)
    monkeypatch.setattr(Path, "unlink", boom_unlink)

    with pytest.raises(ConversionFailed) as exc:
        merge_pdfs([a], dest)
    assert "simulated replace failure" in str(exc.value)


# --- 二轮复审补测：N1/N2/N3，均为审查员实测构造出的输入，13 个既有用例的断言一个字都没改 ---


def _make_encrypted_pdf(path):
    """pypdf 是惰性解析：PdfReader(...) 构造时只读 xref/trailer，一份加密/
    受保护的 PDF 在构造阶段完全正常，只有访问 .pages 时才会炸出
    FileNotDecryptedError。这是 N1 的直接复现素材——租户侧 M365 的敏感度
    标签/IRM 可能让导出的 PDF 带保护，这条路径是真实可达的。"""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password="secret")
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def test_encrypted_shard_raises_conversion_failed_not_bare_pypdf_error(tmp_path):
    """N1：加密分片必须报 ConversionFailed，而不是让 pypdf 的
    FileNotDecryptedError 裸着逃出去——这正是 I1 想在写阶段堵上的同一类
    失败模式，只是这次出现在读阶段。"""
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    encrypted = _make_encrypted_pdf(tmp_path / "encrypted.pdf")
    dest = tmp_path / "merged.pdf"

    with pytest.raises(ConversionFailed) as exc:
        merge_pdfs([a, encrypted], dest)
    assert exc.value.code == "CONVERSION_FAILED"
    assert not dest.exists()


def test_write_phase_non_oserror_wrapped_and_no_orphan_tmp(tmp_path, monkeypatch):
    """N2：writer.write() 除了 IO 层的 OSError（磁盘满等），还可能抛
    pypdf 自身的非 OSError 异常（PdfWriteError/DependencyError/ValueError
    之类）。若写阶段只捕 OSError，这类异常既不会被包装成
    ConversionFailed，也不会触发 tmp 清理——tmp 文件会在输出目录里
    累积成孤儿文件。"""
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    dest = tmp_path / "merged.pdf"

    def boom_write(self, fh):
        raise ValueError("pypdf internal failure, not an OSError")

    monkeypatch.setattr(PdfWriter, "write", boom_write)

    with pytest.raises(ConversionFailed):
        merge_pdfs([a], dest)

    assert not dest.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_readback_failure_wrapped_and_dest_cleaned(tmp_path, monkeypatch):
    """N3：write() 本身不报错，但吐出的字节根本不是合法 PDF——os.replace
    因此顺利完成，dest 落地成功，只有回读阶段才会发现「写出来的东西读不
    出页数」。这道检查恰好是 M1 要新增的价值（回读校验取代内存自证），
    但检出后不能把裸的 pypdf 异常抛出去，也不能把这份已确认读不出来的
    坏文件留在 dest 上（否则用户看到的是一个"存在但打不开"的下载结果）。"""
    a = _make_pdf(tmp_path / "a.pdf", ["P1"])
    dest = tmp_path / "merged.pdf"

    def silently_corrupt_write(self, fh):
        # 不抛异常——模拟"写成功了，但内容本身不是合法 PDF"这种
        # os.replace 会顺利放行、只有回读才能揪出来的失败模式。
        fh.write(b"not actually a pdf, but write() reports success")

    monkeypatch.setattr(PdfWriter, "write", silently_corrupt_write)

    with pytest.raises(ConversionFailed):
        merge_pdfs([a], dest)

    assert not dest.exists()


# --- 三轮复审补测：回读校验必须在 os.replace 之前对 tmp 校验，不能在 replace 之后对 dest 校验 ---


def test_readback_failure_before_replace_preserves_preexisting_dest(tmp_path, monkeypatch):
    """dest 位置预先存在一份上次成功合并的好文件；本次因 write() 静默写出
    非法字节而在回读校验时失败。如果校验点选在 os.replace 之后（对 dest
    回读），失败发生时 dest 早已被替换成坏文件，此时只能在"留一份坏文件"
    和"连旧版本一起删掉"两个更差的选项里选——上一轮选了后者，比前者好，
    但两个都不该被迫选。校验应该在 os.replace 之前对 tmp 回读：write()
    已经成功返回、tmp 已就绪，完全可以先验证再替换，dest 上的旧文件从头
    到尾不会被碰。用 sha256 逐字节比对，确认 dest 真的原封未动。"""
    old = _make_pdf(tmp_path / "old_result_source.pdf", ["OLD"])
    dest = tmp_path / "merged.pdf"
    dest.write_bytes(old.read_bytes())  # 模拟 dest 位置已有一份好结果
    old_digest = hashlib.sha256(dest.read_bytes()).hexdigest()

    a = _make_pdf(tmp_path / "a.pdf", ["P1"])

    def silently_corrupt_write(self, fh):
        fh.write(b"not actually a pdf, but write() reports success")

    monkeypatch.setattr(PdfWriter, "write", silently_corrupt_write)

    with pytest.raises(ConversionFailed):
        merge_pdfs([a], dest)

    assert dest.exists()
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == old_digest
    assert _texts(dest) == ["OLD"]
    assert list(tmp_path.glob("*.tmp")) == []
