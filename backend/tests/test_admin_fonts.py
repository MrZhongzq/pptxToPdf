"""字体管理端点。

字体文件是二进制且有版权，测试里不放真字体：把 font_store.scan_dir
换成桩，只验端点的鉴权、分组、删除语义。解析本身在 test_font_probe.py
与 test_font_store.py 里覆盖。
"""

from datetime import datetime, timezone

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
