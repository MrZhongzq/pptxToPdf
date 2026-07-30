

def test_output_is_settled_when_dest_stem_differs_from_src(tmp_path, monkeypatch):
    """soffice 的 --outdir 输出名是「源文件名换扩展名」，不接受指定。
    调用方给了不同 stem 时也必须拿到文件，而不是「未产出输出文件」。

    这条契约咬过两次：六期一个诊断脚本用了不同 stem，据此得出「未剥离的
    原件 LibreOffice 转不了」的错误结论（实际转出来了，只是叫别的名字）；
    七期的 v1 接口写成 input.pptx -> output.pdf，直接 500。
    """
    from app.services.engines.libreoffice import LibreOfficeEngine

    src = tmp_path / "input.pptx"
    src.write_bytes(b"x")
    produced = tmp_path / "input.pdf"
    produced.write_bytes(b"%PDF-1.4 fake")
    dest = tmp_path / "output.pdf"

    LibreOfficeEngine._settle_output(src, dest)

    assert dest.is_file(), "同目录下 src 同名的产出应被挪到 dest"
    assert not produced.exists()
    assert dest.read_bytes() == b"%PDF-1.4 fake"


def test_settle_output_is_a_noop_when_dest_already_exists(tmp_path):
    """dest 已经就位（同 stem 的常规路径）时不该动它。"""
    from app.services.engines.libreoffice import LibreOfficeEngine

    src = tmp_path / "a.pptx"
    dest = tmp_path / "a.pdf"
    dest.write_bytes(b"original")

    LibreOfficeEngine._settle_output(src, dest)

    assert dest.read_bytes() == b"original"
