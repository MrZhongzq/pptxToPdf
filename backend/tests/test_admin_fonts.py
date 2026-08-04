"""字体管理端点。

字体文件是二进制且有版权，测试里不放真字体：把 font_store.scan_dir
换成桩，只验端点的鉴权、分组、删除语义。解析本身在 test_font_probe.py
与 test_font_store.py 里覆盖。
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import app
from app.services import auth, users
from app.services.font_probe import FontFace
from app.services.font_store import (
    SOURCE_BUILTIN,
    SOURCE_MANAGED,
    SOURCE_MOUNTED,
    FontFile,
    encode_file_id,
)

PASSWORD = "admin-password-123"


def _font(filename: str, source: str, family: str = "微软雅黑") -> FontFile:
    return FontFile(
        file_id=encode_file_id(source, filename),
        filename=filename,
        source=source,
        faces=(FontFace(family=family, style="Regular", version="6.30", index=0),),
        size_bytes=19_000_000,
        charset_count=28_762,
        sha256="a" * 64,
        modified_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    # 与 test_admin_api.py 同理：登录会走 auth._fernet()，没有 secret_key
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
    # app/api/admin.py 的端点走 Depends(get_session)——get_session() 函数体内
    # 对 SessionLocal 是模块内运行时的后绑定查找，所以它天然吃到这份重定向，
    # 不需要单独补丁。这里同样必须拿重定向之后的 db_module.SessionLocal，
    # 才能和 client 走同一个库；模块顶层 import 会拿到重定向之前的旧引用，
    # 写进去的凭证 client 那边读不到。
    import app.db as db_module

    db = db_module.SessionLocal()
    yield db
    db.close()


class TestAuth:
    def test_list_requires_admin(self, client: TestClient) -> None:
        assert client.get("/api/admin/fonts").status_code in (401, 403)

    def test_delete_requires_admin(self, client: TestClient) -> None:
        fid = encode_file_id(SOURCE_MANAGED, "a.ttf")
        assert client.delete(f"/api/admin/fonts/{fid}").status_code in (401, 403)


class TestList:
    def test_groups_by_source(self, admin_session: TestClient, monkeypatch) -> None:
        import app.api.admin_fonts as mod

        monkeypatch.setattr(mod, "_scan_all", lambda include_builtin: [
            _font("managed.ttf", SOURCE_MANAGED),
            _font("mounted.ttf", SOURCE_MOUNTED),
            _font("builtin.ttf", SOURCE_BUILTIN),
        ])
        body = admin_session.get("/api/admin/fonts").json()
        assert [f["filename"] for f in body["managed"]] == ["managed.ttf"]
        assert [f["filename"] for f in body["mounted"]] == ["mounted.ttf"]
        assert [f["filename"] for f in body["builtin"]] == ["builtin.ttf"]

    def test_only_managed_is_deletable(self, admin_session: TestClient, monkeypatch) -> None:
        import app.api.admin_fonts as mod

        monkeypatch.setattr(mod, "_scan_all", lambda include_builtin: [
            _font("managed.ttf", SOURCE_MANAGED),
            _font("mounted.ttf", SOURCE_MOUNTED),
        ])
        body = admin_session.get("/api/admin/fonts").json()
        assert body["managed"][0]["deletable"] is True
        assert body["mounted"][0]["deletable"] is False

    def test_builtin_skipped_by_default(self, admin_session: TestClient, monkeypatch) -> None:
        """内置有 300+ 个，扫一次要秒级，默认不扫。"""
        import app.api.admin_fonts as mod

        seen = {}
        def fake(include_builtin):
            seen["flag"] = include_builtin
            return []
        monkeypatch.setattr(mod, "_scan_all", fake)
        admin_session.get("/api/admin/fonts")
        assert seen["flag"] is False
        admin_session.get("/api/admin/fonts?include_builtin=true")
        assert seen["flag"] is True

    def test_exposes_families_and_readable_version(self, admin_session: TestClient, monkeypatch) -> None:
        import app.api.admin_fonts as mod

        monkeypatch.setattr(mod, "_scan_all", lambda include_builtin: [
            _font("msyh.ttc", SOURCE_MANAGED)
        ])
        item = admin_session.get("/api/admin/fonts").json()["managed"][0]
        assert item["families"] == ["微软雅黑"]
        assert item["version"] == "6.30"
        assert item["charset_count"] == 28_762


class TestScanAll:
    """`_scan_all` 此前完全被 TestList 里的 monkeypatch 桩掉，零覆盖。

    这里直接调用真正的 `_scan_all`，只把最底层的 `scan_dir` 换成记录调用
    参数的桩——不需要真字体文件，也不碰真实的 /usr/share/fonts，
    Windows 上也能跑。
    """

    def _patch(self, monkeypatch, *, builtin_dirs: tuple[Path, ...], mounted_dir: Path, managed_dir: Path):
        import app.api.admin_fonts as mod

        calls: list[tuple[Path, str]] = []

        def fake_scan_dir(directory: Path, source: str) -> list:
            calls.append((directory, source))
            return []

        monkeypatch.setattr(mod, "scan_dir", fake_scan_dir)
        monkeypatch.setattr(mod, "BUILTIN_DIRS", builtin_dirs)
        monkeypatch.setattr(mod, "MOUNTED_DIR", mounted_dir)
        monkeypatch.setattr(mod.settings, "font_dir", managed_dir)
        return mod, calls

    def test_skips_builtin_entirely_when_flag_false(self, tmp_path, monkeypatch) -> None:
        mod, calls = self._patch(
            monkeypatch,
            builtin_dirs=(tmp_path / "builtin",),
            mounted_dir=tmp_path / "mounted",
            managed_dir=tmp_path / "managed",
        )
        (tmp_path / "builtin").mkdir()

        mod._scan_all(include_builtin=False)

        assert all(source != mod.SOURCE_BUILTIN for _, source in calls)

    def test_always_scans_managed_and_mounted(self, tmp_path, monkeypatch) -> None:
        managed_dir = tmp_path / "managed"
        mounted_dir = tmp_path / "mounted"
        mod, calls = self._patch(
            monkeypatch, builtin_dirs=(), mounted_dir=mounted_dir, managed_dir=managed_dir
        )

        mod._scan_all(include_builtin=False)

        assert (managed_dir, mod.SOURCE_MANAGED) in calls
        assert (mounted_dir, mod.SOURCE_MOUNTED) in calls

    def test_scans_builtin_root_and_skips_mounted_subtree(self, tmp_path, monkeypatch) -> None:
        """一次覆盖真机布局验证出的两个点：

        - 根目录本身直接放字体的情况（真机上目前不触发，但不能靠布局
          永远不变来保证），只扫 rglob 命中的子目录会永久漏掉它。
        - mounted 子树可能被用户按字体家族整理成多级子目录，这些子目录
          落在 BUILTIN_DIRS 的 rglob 范围内，必须整棵子树排除，只挡
          MOUNTED_DIR 这一个节点不够。
        """
        builtin_root = tmp_path / "builtin"
        mounted_dir = builtin_root / "truetype" / "extra"
        mounted_child = mounted_dir / "my-family"  # 用户在挂载目录下自建的子目录
        normal_sub = builtin_root / "dejavu"
        for d in (mounted_child, normal_sub):
            d.mkdir(parents=True)

        mod, calls = self._patch(
            monkeypatch,
            builtin_dirs=(builtin_root,),
            mounted_dir=mounted_dir,
            managed_dir=tmp_path / "managed",
        )

        mod._scan_all(include_builtin=True)

        builtin_dirs_scanned = {d for d, source in calls if source == mod.SOURCE_BUILTIN}
        assert builtin_root in builtin_dirs_scanned  # 根目录本身被扫到
        assert normal_sub in builtin_dirs_scanned  # 普通子目录正常扫到
        assert mounted_dir not in builtin_dirs_scanned  # 挂载目录本身不重复归类
        assert mounted_child not in builtin_dirs_scanned  # 挂载目录的子孙同样排除


class TestDelete:
    def test_removes_a_managed_file(self, admin_session: TestClient, tmp_path, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "font_dir", tmp_path)
        target = tmp_path / "a.ttf"
        target.write_bytes(b"x")

        resp = admin_session.delete(f"/api/admin/fonts/{encode_file_id(SOURCE_MANAGED, 'a.ttf')}")

        assert resp.status_code == 204
        assert not target.exists()

    def test_refuses_to_delete_mounted(self, admin_session: TestClient) -> None:
        """手工挂载的目录是 :ro，删了也会失败——提前拦住并说明原因，
        比让 OSError 冒成 500 强。"""
        fid = encode_file_id(SOURCE_MOUNTED, "a.ttf")
        resp = admin_session.delete(f"/api/admin/fonts/{fid}")
        assert resp.status_code == 400
        assert resp.json()["code"] == "FONT_NOT_DELETABLE"

    def test_refuses_to_delete_builtin(self, admin_session: TestClient) -> None:
        """镜像内置字体同样只读，此前只测过 mounted 这一半。"""
        fid = encode_file_id(SOURCE_BUILTIN, "a.ttf")
        resp = admin_session.delete(f"/api/admin/fonts/{fid}")
        assert resp.status_code == 400
        assert resp.json()["code"] == "FONT_NOT_DELETABLE"

    def test_missing_file_is_404_not_silent_success(self, admin_session: TestClient, tmp_path, monkeypatch) -> None:
        """静默成功会让管理员以为删掉了。"""
        from app.config import settings

        monkeypatch.setattr(settings, "font_dir", tmp_path)
        fid = encode_file_id(SOURCE_MANAGED, "nope.ttf")
        resp = admin_session.delete(f"/api/admin/fonts/{fid}")
        assert resp.status_code == 404
        assert resp.json()["code"] == "FONT_NOT_FOUND"

    def test_rejects_path_traversal_in_id(self, admin_session: TestClient, tmp_path, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "font_dir", tmp_path)
        fid = encode_file_id(SOURCE_MANAGED, "../../etc/passwd")
        resp = admin_session.delete(f"/api/admin/fonts/{fid}")
        assert resp.status_code in (400, 404)
