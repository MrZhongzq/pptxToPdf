import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import app
from app.services import admin_auth, graph_credentials

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


@pytest.fixture
def admin_session(client):
    client.post("/api/admin/login", json={"password": PASSWORD})
    return client


@pytest.fixture
def db_session():
    # 延迟导入：conftest.py 的 _isolate_app_db autouse fixture 把
    # app.api.admin.SessionLocal 重定向到了本用例专属的隔离 sqlite 文件
    # （同一份 test_session_local 也绑给了 db_module/pipeline_module 等），
    # 这里必须拿重定向之后的那个名字，才能和 client 走同一个库；模块顶层
    # import 会拿到重定向之前的旧引用，写进去的凭证 client 那边读不到。
    import app.api.admin as admin_module

    db = admin_module.SessionLocal()
    yield db
    db.close()


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


def test_protected_endpoint_rejects_anonymous(client):
    resp = client.get("/api/admin/graph-credentials")
    assert resp.status_code == 401
    assert resp.json()["code"] == "ADMIN_UNAUTHORIZED"


def test_session_slides_on_each_request(client):
    client.post("/api/admin/login", json={"password": PASSWORD})
    first = client.cookies[admin_auth.SESSION_COOKIE_NAME]
    resp = client.get("/api/admin/graph-credentials")
    assert resp.status_code in (200, 404)
    assert "set-cookie" in resp.headers, "每个通过鉴权的请求都应重新签发 cookie"
    assert client.cookies[admin_auth.SESSION_COOKIE_NAME] != first


def test_get_credentials_when_unset(client, admin_session):
    resp = client.get("/api/admin/graph-credentials")
    assert resp.status_code == 200
    body = resp.json()
    assert body["secret_configured"] is False
    assert body["tenant_id"] == ""
    assert body["drive_path"] == "pptx2pdf-staging"


def test_get_credentials_never_returns_secret(client, admin_session, db_session):
    graph_credentials.save_credentials(
        db_session,
        tenant_id="t-1",
        client_id="c-1",
        client_secret="SUPER-SECRET-VALUE",
        site_id="s-1",
        drive_path="staging",
    )
    resp = client.get("/api/admin/graph-credentials")
    body = resp.json()
    assert body["tenant_id"] == "t-1"
    assert body["client_id"] == "c-1"
    assert body["site_id"] == "s-1"
    assert body["drive_path"] == "staging"
    assert body["secret_configured"] is True
    # 密文与明文都不许出现在响应里
    assert "SUPER-SECRET-VALUE" not in resp.text
    assert "client_secret" not in body
    assert "client_secret_encrypted" not in body
