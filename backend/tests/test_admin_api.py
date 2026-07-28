import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import app
from app.services import admin_auth

PASSWORD = "hunter2"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(
        admin_auth.settings, "admin_password_hash", admin_auth.hash_password(PASSWORD)
    )
    monkeypatch.setattr(admin_auth.settings, "admin_cookie_secure", False)
    # conftest.py 没有为 secret_key 提供全局测试值（各测试文件各自设置，
    # 见 test_admin_auth.py 同名注释）。admin_auth 的会话签发/校验依赖
    # settings.secret_key 才能构造 Fernet，这里补上，否则 login 会因
    # AdminNotConfigured 返回 503 而不是 204。
    monkeypatch.setattr(admin_auth.settings, "secret_key", Fernet.generate_key().decode())
    # 口令错误路径的 1 秒延迟在测试里没有意义，抹掉
    monkeypatch.setattr(admin_auth, "_WRONG_PASSWORD_DELAY_S", 0.0)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_login_sets_cookie(client):
    resp = client.post("/api/admin/login", json={"password": PASSWORD})
    assert resp.status_code == 204
    assert admin_auth.SESSION_COOKIE_NAME in resp.cookies


def test_login_rejects_wrong_password(client):
    resp = client.post("/api/admin/login", json={"password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "ADMIN_BAD_PASSWORD"


def test_login_503_when_password_not_configured(client, monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_password_hash", None)
    resp = client.post("/api/admin/login", json={"password": PASSWORD})
    assert resp.status_code == 503
    assert resp.json()["code"] == "ADMIN_NOT_CONFIGURED"


def test_cookie_flags(client):
    resp = client.post("/api/admin/login", json={"password": PASSWORD})
    raw = resp.headers["set-cookie"]
    assert "HttpOnly" in raw
    assert "SameSite=Strict" in raw
    assert "Secure" not in raw


def test_cookie_secure_follows_setting(client, monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_cookie_secure", True)
    resp = client.post("/api/admin/login", json={"password": PASSWORD})
    assert "Secure" in resp.headers["set-cookie"]


def test_logout_clears_cookie_without_auth(client):
    resp = client.post("/api/admin/logout")
    assert resp.status_code == 204
    # 未登录也能登出——语义是「清掉浏览器上的 cookie」，
    # cookie 已过期时同样应该成功
    assert 'pptx2pdf_admin=""' in resp.headers["set-cookie"] or "Max-Age=0" in resp.headers["set-cookie"]


@pytest.mark.xfail(reason="GET 端点在 Task 4")
def test_protected_endpoint_rejects_anonymous(client):
    resp = client.get("/api/admin/graph-credentials")
    assert resp.status_code == 401
    assert resp.json()["code"] == "ADMIN_UNAUTHORIZED"


@pytest.mark.xfail(reason="GET 端点在 Task 4")
def test_session_slides_on_each_request(client):
    client.post("/api/admin/login", json={"password": PASSWORD})
    first = client.cookies[admin_auth.SESSION_COOKIE_NAME]
    resp = client.get("/api/admin/graph-credentials")
    assert resp.status_code in (200, 404)
    assert "set-cookie" in resp.headers, "每个通过鉴权的请求都应重新签发 cookie"
    assert client.cookies[admin_auth.SESSION_COOKIE_NAME] != first
