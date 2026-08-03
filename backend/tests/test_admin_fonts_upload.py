"""字体上传的两步流程：preflight 检查冲突，commit 落盘。

拆两步是为了让冲突弹窗在「文件已传完但还没落盘」时决策——否则要么先
落盘再回滚，要么让管理员重传一遍。
"""

from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import app
from app.services import auth, users
from app.services.font_probe import FontFace
from app.services.font_store import (
    SOURCE_MANAGED,
    SOURCE_MOUNTED,
    FontFile,
    encode_file_id,
)

PASSWORD = "admin-password-123"


def _font(filename: str, source: str, family: str = "微软雅黑", sha256: str = "a" * 64) -> FontFile:
    return FontFile(
        file_id=encode_file_id(source, filename),
        filename=filename,
        source=source,
        faces=(FontFace(family=family, style="Regular", version="6.30", index=0),),
        size_bytes=19_000_000,
        charset_count=28_762,
        sha256=sha256,
        modified_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    # 与 test_admin_fonts.py 同理：登录会走 auth._fernet()，没有 secret_key
    # 会话签发直接 AdminNotConfigured，admin_session fixture 里的登录断言
    # 就过不去——这里跟其它需要登录的测试文件一样各自补上。
    monkeypatch.setattr(auth.settings, "admin_cookie_secure", False)
    monkeypatch.setattr(auth.settings, "secret_key", Fernet.generate_key().decode())
    monkeypatch.setattr(auth, "_WRONG_PASSWORD_DELAY_S", 0.0)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_user(db_session):
    return users.create(
        db_session, username="admin", email="admin@example.com",
        password=PASSWORD, role="admin",
    )


@pytest.fixture
def admin_session(client, admin_user):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


@pytest.fixture
def db_session():
    # 延迟导入：conftest.py 的 _isolate_app_db autouse fixture 把
    # app.db.SessionLocal 重定向到了本用例专属的隔离 sqlite 文件。
    import app.db as db_module

    db = db_module.SessionLocal()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _font_dirs(tmp_path, monkeypatch):
    """所有测试默认把 managed/tmp 目录指向隔离的 tmp_path 子目录，
    避免碰到开发者本机真实的 storage/fonts。个别用例需要不同布局时
    自己再 monkeypatch 覆盖。"""
    from app.config import settings

    monkeypatch.setattr(settings, "font_dir", tmp_path / "fonts")
    monkeypatch.setattr(settings, "font_tmp_dir", tmp_path / "fonts-tmp")


def _upload(client: TestClient, name: str = "test.ttf", data: bytes = b"fake-font-bytes"):
    return client.post(
        "/api/admin/fonts/preflight",
        files={"file": (name, data, "font/ttf")},
    )


class TestPreflightValidation:
    def test_requires_admin(self, client: TestClient) -> None:
        assert _upload(client).status_code in (401, 403)

    def test_rejects_unknown_extension(self, admin_session: TestClient) -> None:
        resp = _upload(admin_session, name="evil.exe")
        assert resp.status_code == 400
        assert resp.json()["code"] == "FONT_INVALID"

    def test_rejects_file_that_is_not_a_font(self, admin_session: TestClient, monkeypatch) -> None:
        """扩展名对但内容不是字体的一样要拦——扩展名是客户端说了算的。"""
        import app.api.admin_fonts as mod

        monkeypatch.setattr(mod, "probe", lambda path, source: None)
        resp = _upload(admin_session)
        assert resp.status_code == 400
        assert resp.json()["code"] == "FONT_INVALID"

    def test_rejects_oversize(self, admin_session: TestClient) -> None:
        """64 MB 上限。pptx 那个 600MB 的限制对字体毫无意义。"""
        resp = _upload(admin_session, data=b"x" * (64 * 1024 * 1024 + 1))
        assert resp.status_code == 413
        assert resp.json()["code"] == "FONT_TOO_LARGE"

    def test_leaves_no_temp_file_when_rejected(self, admin_session: TestClient, tmp_path, monkeypatch) -> None:
        """被拒的上传不能在暂存区留垃圾。

        settings.font_tmp_dir 是普通字段，只在 Settings() 构造时按
        storage_root / font_tmp_dirname 算一次默认值（见 app/config.py 里
        _default_font_dirs 的注释），构造完之后再改 storage_root /
        font_tmp_dirname 不会让它跟着变。必须直接 monkeypatch font_tmp_dir
        本身，否则断言检查的是一个从未被写入过的目录，不管清理逻辑对不
        对，Path.glob 在目录不存在时也只是安静地返回空，测试永远绿。
        """
        from app.config import settings
        import app.api.admin_fonts as mod

        monkeypatch.setattr(settings, "font_tmp_dir", tmp_path / "t")
        monkeypatch.setattr(mod, "probe", lambda path, source: None)
        _upload(admin_session)
        assert list((tmp_path / "t").glob("*")) == []


class TestPreflightConflicts:
    def test_duplicate_sha_is_reported_and_not_a_conflict(self, admin_session, tmp_path, monkeypatch) -> None:
        """完全相同的文件没有歧义，直接告知，不弹冲突框。"""
        import app.api.admin_fonts as mod

        data = b"fake-font-bytes"
        # 已有文件的 sha256 必须与上传内容真实一致——照抄 probe() 的算法
        # （sha256_of 就是 hashlib.sha256 逐块喂），而不是随手编一个假
        # 字符串，否则测的是「桩返回了什么」而不是端点真正比对 sha256 的
        # 逻辑。
        import hashlib

        real_sha = hashlib.sha256(data).hexdigest()
        dup = _font("existing.ttf", SOURCE_MANAGED, sha256=real_sha)
        monkeypatch.setattr(mod, "_scan_all", lambda include_builtin: [dup])
        # 测试机没有 fontconfig，真正的 probe() 解不出假字节——桩一个
        # sha256 与 dup 相同的 FontFile，只验证端点比对 sha256 的分支。
        monkeypatch.setattr(
            mod, "probe", lambda path, source: _font(path.name, source, sha256=real_sha)
        )

        resp = _upload(admin_session, data=data)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["duplicate_of"]["filename"] == "existing.ttf"
        assert body["candidates"] == []

    def test_family_overlap_returns_candidates(self, admin_session, monkeypatch) -> None:
        import app.api.admin_fonts as mod

        # family 相同、sha256 不同（否则会先命中 duplicate 分支）
        clash = _font("existing.ttf", SOURCE_MANAGED, family="微软雅黑", sha256="b" * 64)
        monkeypatch.setattr(mod, "_scan_all", lambda include_builtin: [clash])
        # probe 的桩返回一个与 clash 同 family 的 FontFile，模拟「新上传的
        # 文件解析出与已有文件相同的 family」
        monkeypatch.setattr(
            mod, "probe",
            lambda path, source: _font(path.name, source, family="微软雅黑", sha256="c" * 64),
        )

        resp = _upload(admin_session)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["duplicate_of"] is None
        assert [c["filename"] for c in body["candidates"]] == ["existing.ttf"]

    def test_no_overlap_returns_empty_candidates(self, admin_session, monkeypatch) -> None:
        import app.api.admin_fonts as mod

        unrelated = _font("other.ttf", SOURCE_MANAGED, family="Arial", sha256="b" * 64)
        monkeypatch.setattr(mod, "_scan_all", lambda include_builtin: [unrelated])
        monkeypatch.setattr(
            mod, "probe",
            lambda path, source: _font(path.name, source, family="微软雅黑", sha256="c" * 64),
        )

        resp = _upload(admin_session)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["duplicate_of"] is None
        assert body["candidates"] == []


class TestStagedDirTraversal:
    """schema 的 pattern 已经在请求体这一层挡掉了非法 token（见
    schemas.py 的 FontCommitRequest.token），所以走 HTTP 端点测不出
    _staged_dir 自己这道防线——请求根本进不到端点里。这里直接单测
    _staged_dir(token)，验证的是「schema 那道以后被重构弄丢了」的场景。
    """

    @pytest.mark.parametrize(
        "token",
        [
            "/etc",
            "../../../etc",
            "ABC0123456789abcdef0123456789abc",  # 大写，格式对但不匹配 [0-9a-f]
            "",
        ],
    )
    def test_traversal_and_malformed_tokens_rejected(self, token: str) -> None:
        from app.config import settings
        from app.errors import FontUploadExpired
        import app.api.admin_fonts as mod

        with pytest.raises(FontUploadExpired):
            mod._staged_dir(token)

        # 没有任何东西被写进 font_dir——_staged_dir 只负责算路径,
        # 但这里额外确认拒绝发生在任何文件系统操作之前。
        assert not settings.font_dir.exists() or list(settings.font_dir.glob("*")) == []


def _mock_probe(monkeypatch) -> None:
    """测试机没有 fontconfig（Windows 上跑不了 fc-query），真正的 probe()
    对 `b"fake-font-bytes"` 只会返回 None。TestCommit 关心的是落盘/替换/
    改名这些文件系统语义，不是字体解析本身（那部分在 test_font_probe.py
    与 test_font_store.py 里覆盖），所以把 probe 换成按路径现造一个
    FontFile 的桩——filename 用真实路径名，保证 preflight 与 commit 两次
    调用（分别在写入前与落盘后）返回互相一致的结果。"""
    import app.api.admin_fonts as mod

    monkeypatch.setattr(mod, "probe", lambda path, source: _font(path.name, source))


class TestCommit:
    def test_writes_file_and_returns_it(self, admin_session, tmp_path, monkeypatch) -> None:
        from app.config import settings

        _mock_probe(monkeypatch)
        pre = _upload(admin_session)
        assert pre.status_code == 200, pre.text
        token = pre.json()["token"]

        resp = admin_session.post("/api/admin/fonts", json={"token": token, "replace": []})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["filename"] == "test.ttf"
        assert (settings.font_dir / "test.ttf").is_file()
        assert (settings.font_dir / "test.ttf").read_bytes() == b"fake-font-bytes"

    def test_replaces_listed_files(self, admin_session, tmp_path, monkeypatch) -> None:
        from app.config import settings

        _mock_probe(monkeypatch)
        pre = _upload(admin_session)
        assert pre.status_code == 200, pre.text
        token = pre.json()["token"]

        settings.font_dir.mkdir(parents=True, exist_ok=True)
        old = settings.font_dir / "old.ttf"
        old.write_bytes(b"old-bytes")
        old_id = encode_file_id(SOURCE_MANAGED, "old.ttf")

        resp = admin_session.post(
            "/api/admin/fonts", json={"token": token, "replace": [old_id]}
        )

        assert resp.status_code == 200, resp.text
        assert not old.exists()
        assert (settings.font_dir / "test.ttf").is_file()
        assert (settings.font_dir / "test.ttf").read_bytes() == b"fake-font-bytes"

    def test_refuses_to_replace_non_managed(self, admin_session, monkeypatch) -> None:
        """replace 列表里有手工挂载/内置的 → 400，不静默跳过。
        静默跳过会让管理员以为替换成功了。"""
        from app.config import settings

        _mock_probe(monkeypatch)
        pre = _upload(admin_session)
        assert pre.status_code == 200, pre.text
        token = pre.json()["token"]

        # replace 列表里混一个合法的 managed 文件——如果实现是「遍历到哪个
        # 非法就在哪短路，之前遍历过的合法条目已经删了」，这个断言能抓到；
        # 只有整体先校验完、一个不合法就全部不动手才能通过。
        settings.font_dir.mkdir(parents=True, exist_ok=True)
        old = settings.font_dir / "old.ttf"
        old.write_bytes(b"old-bytes")
        old_id = encode_file_id(SOURCE_MANAGED, "old.ttf")
        mounted_id = encode_file_id(SOURCE_MOUNTED, "a.ttf")

        resp = admin_session.post(
            "/api/admin/fonts", json={"token": token, "replace": [old_id, mounted_id]}
        )

        assert resp.status_code == 400
        assert resp.json()["code"] == "FONT_NOT_DELETABLE"
        assert old.read_bytes() == b"old-bytes"
        assert list(settings.font_dir.glob("*")) == [old]

    def test_probe_failure_at_commit_has_no_side_effects(
        self, admin_session, tmp_path, monkeypatch
    ) -> None:
        """commit 阶段 probe 失败 → 400，且不会一半替换一半失败：旧文件
        还在，没有新文件被写进 font_dir。这是本轮最重要的一条——证明
        probe 提前到任何破坏性操作之前真的生效了，不是「先删后探测」。
        """
        from app.config import settings
        import app.api.admin_fonts as mod

        _mock_probe(monkeypatch)
        pre = _upload(admin_session)
        assert pre.status_code == 200, pre.text
        token = pre.json()["token"]

        settings.font_dir.mkdir(parents=True, exist_ok=True)
        old = settings.font_dir / "old.ttf"
        old.write_bytes(b"old-bytes")
        old_id = encode_file_id(SOURCE_MANAGED, "old.ttf")

        # 模拟暂存文件在 preflight 之后、commit 之前损坏/不可解析：
        # preflight 已经走过 _mock_probe 成功了，这里把 probe 换成
        # commit 阶段会调用的失败桩。
        monkeypatch.setattr(mod, "probe", lambda path, source: None)

        resp = admin_session.post(
            "/api/admin/fonts", json={"token": token, "replace": [old_id]}
        )

        assert resp.status_code == 400
        assert resp.json()["code"] == "FONT_INVALID"
        assert old.read_bytes() == b"old-bytes"
        assert list(settings.font_dir.glob("*")) == [old]

    def test_expired_token_is_410(self, admin_session) -> None:
        """格式合法但目录不在——已过期或从未存在，对调用方是同一件事。"""
        resp = admin_session.post(
            "/api/admin/fonts", json={"token": "f" * 32, "replace": []}
        )
        assert resp.status_code == 410
        assert resp.json()["code"] == "FONT_UPLOAD_EXPIRED"

    def test_malformed_token_is_rejected_by_schema(self, admin_session) -> None:
        """格式非法在 schema 层就被拦下，进不到端点。

        「不存在」与「已过期」必须不可区分（都是 410），但「格式错」
        和它们区分开没有害处——攻击者拿不到任何有价值的信息。
        """
        resp = admin_session.post(
            "/api/admin/fonts", json={"token": "does-not-exist", "replace": []}
        )
        assert resp.status_code == 422

    def test_renames_on_collision(self, admin_session, tmp_path, monkeypatch) -> None:
        from app.config import settings

        _mock_probe(monkeypatch)
        pre = _upload(admin_session)
        assert pre.status_code == 200, pre.text
        token = pre.json()["token"]

        # 目标目录里已经有个同名但没被列进 replace 的文件——不是替换,
        # 是撞名, resolve_collision 应该把新文件改名而不是覆盖旧文件。
        settings.font_dir.mkdir(parents=True, exist_ok=True)
        existing = settings.font_dir / "test.ttf"
        existing.write_bytes(b"unrelated-old-content")

        resp = admin_session.post("/api/admin/fonts", json={"token": token, "replace": []})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["filename"] != "test.ttf"
        assert existing.read_bytes() == b"unrelated-old-content"
        assert (settings.font_dir / body["filename"]).read_bytes() == b"fake-font-bytes"
