"""字体目录的扫描、文件名安全处理与冲突判定。

冲突判定是纯函数，不碰文件系统，所以这里的用例全部用构造的 FontFile。
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services import font_store
from app.services.font_probe import FontFace
from app.services.font_store import (
    SOURCE_BUILTIN,
    SOURCE_MANAGED,
    SOURCE_MOUNTED,
    FontFile,
    decode_file_id,
    encode_file_id,
    find_conflicts,
    is_duplicate,
    probe,
    resolve_collision,
    safe_filename,
)


def _font(filename: str, families: list[str], *, sha: str = "aa", source: str = SOURCE_MANAGED) -> FontFile:
    return FontFile(
        file_id=encode_file_id(source, filename),
        filename=filename,
        source=source,
        faces=tuple(FontFace(family=f, style="Regular", version="1.00", index=i)
                    for i, f in enumerate(families)),
        size_bytes=1024,
        charset_count=100,
        sha256=sha,
        modified_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )


class TestFileId:
    def test_round_trips(self) -> None:
        fid = encode_file_id(SOURCE_MANAGED, "msyh.ttc")
        assert decode_file_id(fid) == (SOURCE_MANAGED, "msyh.ttc")

    def test_survives_non_ascii_and_slashes_in_name(self) -> None:
        """中文文件名与看起来像路径的名字都要能原样还原，
        否则删除时会找不到文件。"""
        fid = encode_file_id(SOURCE_MANAGED, "思源黑体 Regular.otf")
        assert decode_file_id(fid) == (SOURCE_MANAGED, "思源黑体 Regular.otf")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            decode_file_id("!!!not-base64!!!")

    def test_rejects_absolute_path_in_filename(self) -> None:
        """partition 只切第一个斜杠——"managed//etc/passwd" 会解出一个
        绝对路径，而 pathlib 的 `base / "/etc/passwd"` 会整体替换掉 base。
        """
        import base64
        evil = base64.urlsafe_b64encode(b"managed//etc/passwd").decode().rstrip("=")
        with pytest.raises(ValueError):
            decode_file_id(evil)

    def test_rejects_traversal_in_filename(self) -> None:
        import base64
        evil = base64.urlsafe_b64encode(b"managed/../../etc/passwd").decode().rstrip("=")
        with pytest.raises(ValueError):
            decode_file_id(evil)

    def test_rejects_unknown_source(self) -> None:
        import base64
        evil = base64.urlsafe_b64encode(b"../../etc/font.ttf").decode().rstrip("=")
        with pytest.raises(ValueError):
            decode_file_id(evil)


class TestSafeFilename:
    def test_strips_directory_components(self) -> None:
        """路径穿越：写进字体目录之外是绝对不能发生的。"""
        assert safe_filename("../../etc/passwd") == "passwd"
        assert safe_filename("/abs/path/font.ttf") == "font.ttf"
        assert safe_filename("..\\\\windows\\\\font.ttf") == "font.ttf"

    def test_replaces_illegal_characters(self) -> None:
        assert safe_filename('a<b>c:d"e|f?g*.ttf') == "a_b_c_d_e_f_g_.ttf"

    def test_keeps_chinese_and_spaces(self) -> None:
        assert safe_filename("思源黑体 Regular.otf") == "思源黑体 Regular.otf"

    def test_falls_back_when_name_becomes_empty(self) -> None:
        assert safe_filename("...") == "font"
        assert safe_filename("") == "font"


class TestResolveCollision:
    def test_returns_name_unchanged_when_free(self, tmp_path: Path) -> None:
        assert resolve_collision(tmp_path, "a.ttf") == "a.ttf"

    def test_appends_incrementing_suffix_before_extension(self, tmp_path: Path) -> None:
        (tmp_path / "a.ttf").write_bytes(b"x")
        assert resolve_collision(tmp_path, "a.ttf") == "a-2.ttf"
        (tmp_path / "a-2.ttf").write_bytes(b"x")
        assert resolve_collision(tmp_path, "a.ttf") == "a-3.ttf"


class TestDuplicate:
    def test_same_sha_is_a_duplicate(self) -> None:
        existing = [_font("old.ttf", ["A"], sha="deadbeef")]
        assert is_duplicate("deadbeef", existing).filename == "old.ttf"

    def test_different_sha_is_not(self) -> None:
        existing = [_font("old.ttf", ["A"], sha="deadbeef")]
        assert is_duplicate("cafe", existing) is None


class TestFindConflicts:
    def test_matches_on_family_regardless_of_style(self) -> None:
        """style 各家命名不统一，只能看 family。"""
        incoming = _font("new.ttf", ["微软雅黑"])
        existing = [_font("old.ttf", ["微软雅黑"])]
        assert [c.filename for c in find_conflicts(incoming, existing)] == ["old.ttf"]

    def test_any_face_matching_makes_the_whole_file_a_candidate(self) -> None:
        """ttc 的任一 face 撞了，整个文件就是候选——替换只能整文件来。"""
        incoming = _font("new.ttc", ["Noto Sans CJK JP", "Noto Sans CJK SC"])
        existing = [_font("old.ttc", ["完全无关", "Noto Sans CJK SC"])]
        assert len(find_conflicts(incoming, existing)) == 1

    def test_no_overlap_means_no_conflict(self) -> None:
        incoming = _font("new.ttf", ["A"])
        existing = [_font("old.ttf", ["B"])]
        assert find_conflicts(incoming, existing) == []

    def test_includes_mounted_and_builtin_as_candidates(self) -> None:
        """手工挂载与内置的也要列出来——它们删不掉，但管理员需要知道
        名字被占了、自己传的可能不生效。"""
        incoming = _font("new.ttf", ["微软雅黑"])
        existing = [
            _font("m.ttf", ["微软雅黑"], source=SOURCE_MOUNTED),
            _font("b.ttf", ["微软雅黑"], source=SOURCE_BUILTIN),
        ]
        assert len(find_conflicts(incoming, existing)) == 2

    def test_does_not_report_the_file_against_itself(self) -> None:
        """同一个文件（file_id 相同）不算与自己冲突。"""
        incoming = _font("same.ttf", ["A"])
        assert find_conflicts(incoming, [incoming]) == []


class TestProbeCallsFcQuery:
    """probe() 调 fc-query 时用的 format 字符串本身要被锁住。

    font_probe.py 里 parse_charset 的多段测试只测「多段输入怎么算」，
    测不到「fc-query 真的会不会分段输出」——那半条逻辑完全靠 probe()
    传给 fc-query 的 --format 参数。真机上一个 10 face 的 Noto CJK 显示
    「覆盖 -462,222,757 字」，根因就是这个 format 当初没带 \n：ttc 一个
    文件多个 face，fc-query 为每个 face 各输出一段，没有 \n 分隔的话
    相邻两段会粘成一个畸形 token（前段末尾 ...ffff 接后段开头 20-7e
    变成 ffff20-7e），减法算出巨大负数。两半（「怎么算」与「怎么分段」）
    都要有测试锁住，否则谁把 CHARSET_FORMAT 的 \n 删掉，全量测试照样
    全绿，bug 原样复现。
    """

    def test_charset_format_asks_fc_query_to_separate_faces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        class _Result:
            returncode = 0
            stdout = "DejaVu Sans\tBook\t155320\t0\n"

        def fake_run(cmd: list[str], **kwargs: object) -> _Result:
            calls.append(cmd)
            return _Result()

        monkeypatch.setattr(font_store.subprocess, "run", fake_run)

        font_path = tmp_path / "a.ttf"
        font_path.write_bytes(b"fake font bytes")

        result = probe(font_path, SOURCE_MANAGED)

        assert result is not None
        assert len(calls) == 2  # 一次取 meta（family/style/...），一次取 charset

        charset_calls = [c for c in calls if "%{charset}" in " ".join(c)]
        assert len(charset_calls) == 1, "probe 应该只查询一次 charset"
        cmd = charset_calls[0]
        fmt = cmd[cmd.index("--format") + 1]
        assert fmt.endswith("\n"), (
            f"charset 的 --format 必须以换行结尾，让 fc-query 按 face 分段输出，"
            f"否则 ttc 多 face 会粘连出畸形 token 并算出负数；实际传的是 {fmt!r}"
        )
