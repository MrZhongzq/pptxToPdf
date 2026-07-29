import zipfile
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches, Pt

from app.services import media_strip
from app.services.media_strip import MEDIA_REL_TYPES, strip_media
from app.services.opc_rewrite import read_rels

VIDEO_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/video"
MEDIA_REL = "http://schemas.microsoft.com/office/2007/relationships/media"


def _plain_deck(path: Path, pages: int = 3) -> Path:
    """不含媒体的普通 deck。"""
    prs = Presentation()
    for i in range(pages):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
        box.text_frame.text = f"PAGE-{i + 1}"
        box.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
    prs.save(str(path))
    return path


def _deck_with_fake_video(path: Path, payload_mb: int = 2) -> Path:
    """造一个带假视频 part 的 deck。

    python-pptx 没有直接加视频的稳定 API，所以先造普通 deck，再用
    zipfile 手工塞入 media part、在 slide1 的 rels 里加两条关系
    （video + media，PowerPoint 真实产出就是两条都写），并往
    [Content_Types].xml 加一条 Default。
    """
    tmp = path.with_suffix(".base.pptx")
    _plain_deck(tmp, pages=3)

    payload = b"\x00" * (payload_mb * 1024 * 1024)
    with zipfile.ZipFile(tmp) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            raw = zin.read(item.filename)
            if item.filename == "ppt/slides/_rels/slide1.xml.rels":
                injected = (
                    f'<Relationship Id="rIdVid" Type="{VIDEO_REL}" Target="../media/movie1.mp4"/>'
                    f'<Relationship Id="rIdMed" Type="{MEDIA_REL}" Target="../media/movie1.mp4"/>'
                ).encode()
                raw = raw.replace(b"</Relationships>", injected + b"</Relationships>")
            elif item.filename == "[Content_Types].xml":
                raw = raw.replace(
                    b"</Types>",
                    b'<Default Extension="mp4" ContentType="video/mp4"/></Types>',
                )
            zout.writestr(item, raw)
        zout.writestr("ppt/media/movie1.mp4", payload)

    tmp.unlink()
    return path


def _parts(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


def test_media_rel_types_covers_video_audio_and_media():
    # PowerPoint 嵌一段视频会同时写 video 与 media 两条关系指向同一个 part，
    # 漏一个文件就还留在包里
    assert VIDEO_REL in MEDIA_REL_TYPES
    assert MEDIA_REL in MEDIA_REL_TYPES
    assert any("audio" in t for t in MEDIA_REL_TYPES)


def test_strips_video_part(tmp_path):
    deck = _deck_with_fake_video(tmp_path / "v.pptx", payload_mb=2)
    before = _parts(deck)
    assert "ppt/media/movie1.mp4" in before

    result = strip_media(deck)

    assert result.stripped is True
    assert result.removed_parts == 1
    assert result.bytes_after < result.bytes_before
    assert "ppt/media/movie1.mp4" not in _parts(deck)


def test_keeps_all_slides(tmp_path):
    deck = _deck_with_fake_video(tmp_path / "v.pptx")
    strip_media(deck)
    slides = [n for n in _parts(deck) if n.startswith("ppt/slides/slide")]
    assert len(slides) == 3


def test_no_dangling_internal_relationships(tmp_path):
    deck = _deck_with_fake_video(tmp_path / "v.pptx")
    strip_media(deck)
    with zipfile.ZipFile(deck) as zf:
        names = set(zf.namelist())
        for name in list(names):
            if not name.endswith(".rels"):
                continue
            owner = name.replace("/_rels/", "/").replace(".rels", "")
            for _rid, _type, target in read_rels(zf, owner):
                assert target in names, f"{name} 指向不存在的 {target}"


def test_result_is_openable(tmp_path):
    deck = _deck_with_fake_video(tmp_path / "v.pptx")
    strip_media(deck)
    prs = Presentation(str(deck))
    assert len(prs.slides) == 3


def test_deck_without_media_is_not_rewritten(tmp_path):
    deck = _plain_deck(tmp_path / "p.pptx")
    before_bytes = deck.read_bytes()

    result = strip_media(deck)

    assert result.stripped is False
    assert result.removed_parts == 0
    # 零收益时不该白做一次解压重打包
    assert deck.read_bytes() == before_bytes


def test_preserves_mc_ignorable(tmp_path):
    """ET 往返会丢弃未使用的 xmlns 声明，而 mc:Ignorable 指着它们。
    presentation.xml 必须逐字节保留——这是三期修了一整轮的坑。"""
    deck = _deck_with_fake_video(tmp_path / "v.pptx")
    with zipfile.ZipFile(deck) as zf:
        pres_before = zf.read("ppt/presentation.xml")

    strip_media(deck)

    with zipfile.ZipFile(deck) as zf:
        assert zf.read("ppt/presentation.xml") == pres_before


def test_write_failure_cleans_up_tmp_and_preserves_source(tmp_path, monkeypatch):
    """写入过程中失败时，临时文件要被清理，源文件不能被破坏。

    originals_dir 目前没有其它机制会兜底清理孤儿 tmp 文件——drop_original
    只精确删 {task_id}.pptx，purge_expired_outputs/purge_expired_shards 都不
    扫这里——所以 strip_media 的 except 分支必须自己证明语义正确：异常穿透
    给调用方，tmp 文件不留下，源文件全程未被替换。
    """
    deck = _deck_with_fake_video(tmp_path / "v.pptx")
    before_bytes = deck.read_bytes()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(media_strip, "rewrite_content_types", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        strip_media(deck)

    # 源文件全程未被 tmp.replace(src) 替换，字节不变
    assert deck.read_bytes() == before_bytes
    # 没有留下孤儿 tmp 文件
    leftover = [p for p in tmp_path.iterdir() if p != deck and p.suffix == ".pptx"]
    assert leftover == []
