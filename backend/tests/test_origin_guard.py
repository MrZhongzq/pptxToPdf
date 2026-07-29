"""防跨站中间件与白名单管理。

这个功能默认关闭，但**默认关闭本身就是要被测试钉住的行为**——
需求明确说「现在没用，先做着」，如果哪天它悄悄自己生效了，表现是整站
写请求全 403，而且第一反应不会是「那个没启用的功能」。
"""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import app
from app.models import AllowedOrigin
from app.services import auth, origin_guard, users

PASSWORD = "hunter2!"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_cookie_secure", False)
    monkeypatch.setattr(auth.settings, "secret_key", Fernet.generate_key().decode())
    monkeypatch.setattr(auth, "_WRONG_PASSWORD_DELAY_S", 0.0)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    import app.db as db_module

    s = db_module.SessionLocal()
    yield s
    s.close()


@pytest.fixture
def admin_client(client, db):
    users.create(db, username="admin", email="a@example.com", password=PASSWORD, role="admin")
    client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    return client


# ---- 纯函数 ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://example.com", "example.com"),
        ("https://example.com:8443", "example.com:8443"),
        ("http://192.168.1.5:18993/some/path", "192.168.1.5:18993"),
        ("HTTP://EXAMPLE.COM", "example.com"),
        ("example.com", "example.com"),
        (None, None),
        ("", None),
    ],
)
def test_extract_host(raw, expected):
    assert origin_guard.extract_host(raw) == expected


def test_protocol_is_ignored():
    """同一台机器的 http 与 https 是同一个来源，让管理员为此配两条
    只会平添困惑。"""
    assert origin_guard.extract_host("http://a.com") == origin_guard.extract_host("https://a.com")


def test_missing_origin_is_allowed():
    """同源表单、curl、部分浏览器隐私模式都不带 Origin/Referer。把「没有
    头」当成跨站会误伤大量正常请求，而真正的跨站场景浏览器一定会带。"""
    assert origin_guard.is_allowed(None, {"a.com"}) is True


def test_only_write_methods_are_guarded():
    for m in ("POST", "PUT", "PATCH", "DELETE"):
        assert origin_guard.should_check(m) is True
    for m in ("GET", "HEAD", "OPTIONS"):
        assert origin_guard.should_check(m) is False


# ---- 中间件：默认关闭 ----


def test_disabled_by_default(client, db):
    """需求说「这个功能现在没用」。默认值一旦被改成 True，整站写请求
    会在没有白名单的部署上全部 403。"""
    from app.config import settings

    assert settings.origin_guard_enabled is False


def test_disabled_lets_foreign_origin_through(client):
    resp = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "whatever"},
        headers={"Origin": "http://evil.example"},
    )
    # 401（凭据错）而不是 403（跨站）——说明请求根本没被中间件拦
    assert resp.status_code == 401
    assert resp.json()["code"] == "BAD_CREDENTIALS"


# ---- 中间件：启用 ----


def test_enabled_but_empty_whitelist_lets_everything_through(client, monkeypatch):
    """第二道保险。没有这条，第一次打开开关就会把所有写请求——包括
    管理员用来添加第一条白名单的那次请求——全部拒绝，变成无法自救的死锁。
    """
    monkeypatch.setattr("app.config.settings.origin_guard_enabled", True)
    resp = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "whatever"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 401


def test_enabled_blocks_origin_outside_whitelist(client, db, monkeypatch):
    db.add(AllowedOrigin(origin_id="o1", origin="good.example"))
    db.commit()
    monkeypatch.setattr("app.config.settings.origin_guard_enabled", True)

    resp = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "whatever"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "CROSS_ORIGIN_BLOCKED"


def test_enabled_allows_origin_in_whitelist(client, db, monkeypatch):
    db.add(AllowedOrigin(origin_id="o1", origin="good.example"))
    db.commit()
    monkeypatch.setattr("app.config.settings.origin_guard_enabled", True)

    resp = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "whatever"},
        headers={"Origin": "http://good.example"},
    )
    assert resp.status_code == 401  # 过了中间件，倒在凭据校验上


def test_enabled_still_allows_reads(client, db, monkeypatch):
    """拦 GET 只会破坏图片、下载等正常用法，收益为零。"""
    db.add(AllowedOrigin(origin_id="o1", origin="good.example"))
    db.commit()
    monkeypatch.setattr("app.config.settings.origin_guard_enabled", True)

    resp = client.get("/api/auth/me", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 200


def test_falls_back_to_referer_when_origin_absent(client, db, monkeypatch):
    db.add(AllowedOrigin(origin_id="o1", origin="good.example"))
    db.commit()
    monkeypatch.setattr("app.config.settings.origin_guard_enabled", True)

    resp = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "whatever"},
        headers={"Referer": "http://evil.example/page"},
    )
    assert resp.status_code == 403


# ---- 白名单管理端点 ----


def test_list_origins_requires_admin(client):
    assert client.get("/api/admin/origins").status_code == 401


def test_create_and_list_origin(admin_client):
    resp = admin_client.post("/api/admin/origins", json={"origin": "a.example", "note": "测试"})
    assert resp.status_code == 201
    assert resp.json()["origin"] == "a.example"

    rows = admin_client.get("/api/admin/origins").json()
    assert [r["origin"] for r in rows] == ["a.example"]


def test_origin_is_normalized(admin_client):
    resp = admin_client.post("/api/admin/origins", json={"origin": "  A.Example  "})
    assert resp.json()["origin"] == "a.example"


def test_rejects_origin_with_scheme(admin_client):
    """带协议的话永远匹配不上（比对只看 host[:port]）。宁可当场拒绝，
    也不要让人配了一条永不生效的规则还以为自己配好了。"""
    resp = admin_client.post("/api/admin/origins", json={"origin": "http://a.example"})
    assert resp.status_code == 422


def test_rejects_duplicate_origin(admin_client):
    admin_client.post("/api/admin/origins", json={"origin": "a.example"})
    assert admin_client.post("/api/admin/origins", json={"origin": "a.example"}).status_code == 422


def test_delete_origin(admin_client):
    created = admin_client.post("/api/admin/origins", json={"origin": "a.example"}).json()
    assert admin_client.delete(f"/api/admin/origins/{created['origin_id']}").status_code == 204
    assert admin_client.get("/api/admin/origins").json() == []
