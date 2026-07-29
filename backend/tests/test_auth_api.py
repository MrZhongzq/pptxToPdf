"""账号端点：登录、登出、me，以及会话的生效与失效。

四期这些用例住在 test_admin_api.py 里，验的是「单一口令 + cookie」。
六期换成账号体系后整体搬到这里并重写，但**每一条四期的保护都要有对应的
新表达**，不能借着重构把断言弄丢——尤其是「关门」那条，见
test_suspended_user_session_dies_immediately。
"""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import app
from app.services import auth, users

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
def db_session():
    import app.db as db_module

    db = db_module.SessionLocal()
    yield db
    db.close()


@pytest.fixture
def admin(db_session):
    return users.create(
        db_session, username="admin", email="a@example.com", password=PASSWORD, role="admin"
    )


@pytest.fixture
def member(db_session):
    return users.create(
        db_session, username="alice", email="alice@example.com", password=PASSWORD, role="user"
    )


def _login(client, username="admin", password=PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ---- 登录 ----


def test_login_sets_cookie(client, admin):
    resp = _login(client)
    assert resp.status_code == 200
    assert auth.SESSION_COOKIE_NAME in resp.cookies
    assert resp.json()["username"] == "admin"
    assert resp.json()["role"] == "admin"


def test_login_response_never_leaks_password_hash(client, admin):
    """哈希不是秘密，但把它发到浏览器等于把离线爆破的入场券送出去，
    而它没有任何前端用途。"""
    body = resp = _login(client).json()
    assert "password_hash" not in body
    assert "password" not in str(resp)


def test_login_rejects_wrong_password(client, admin):
    resp = _login(client, password="wrong")
    assert resp.status_code == 401
    assert resp.json()["code"] == "BAD_CREDENTIALS"


def test_login_rejects_unknown_user_with_same_code(client, admin):
    """「用户不存在」与「密码错误」必须返回同一个码——区分等于把用户名
    枚举送给攻击者。"""
    resp = _login(client, username="nobody")
    assert resp.status_code == 401
    assert resp.json()["code"] == "BAD_CREDENTIALS"


def test_login_is_case_insensitive_on_username(client, admin):
    """Admin 与 admin 在人眼里是同一个账号，登录也该如此——否则「用户名
    唯一」这条保证在用户看来是假的。"""
    assert _login(client, username="ADMIN").status_code == 200


def test_suspended_user_cannot_login(client, db_session, member):
    users.set_status(db_session, member.user_id, "suspended", actor_id="someone-else")
    resp = _login(client, username="alice")
    assert resp.status_code == 401


# ---- 会话 ----


def test_suspended_user_session_dies_immediately(client, db_session, member):
    """四期那条「清空口令后旧 cookie 立刻失效」在六期的等价保护。

    如果 require_user 只验签名不回库查状态，暂停一个账号最多要等
    admin_session_days（默认 3 天）才真正生效——那这个功能就是假的。
    """
    assert _login(client, username="alice").status_code == 200
    assert client.get("/api/auth/me").json()["username"] == "alice"

    users.set_status(db_session, member.user_id, "suspended", actor_id="someone-else")

    assert client.get("/api/auth/me").json() is None


def test_deleted_user_session_dies_immediately(client, db_session, member):
    """同上，删除也必须立刻生效。"""
    assert _login(client, username="alice").status_code == 200
    users.delete(db_session, member.user_id, actor_id="someone-else")
    assert client.get("/api/auth/me").json() is None


def test_session_slides_on_each_request(client, admin):
    _login(client)
    first = client.cookies[auth.SESSION_COOKIE_NAME]
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert "set-cookie" in resp.headers, "每个带有效会话的请求都应重新签发 cookie"
    assert client.cookies[auth.SESSION_COOKIE_NAME] != first


def test_cookie_flags(client, admin):
    raw = _login(client).headers["set-cookie"]
    assert "HttpOnly" in raw
    assert "SameSite=Strict" in raw
    assert "Secure" not in raw


def test_cookie_secure_follows_setting(client, admin, monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_cookie_secure", True)
    assert "Secure" in _login(client).headers["set-cookie"]


# ---- me / logout ----


def test_me_returns_null_when_anonymous(client):
    """未登录返回 null 而不是 401——这是完全正常的状态，用 401 表达会让
    前端把「正常的未登录」和「会话过期」写成同一条错误分支。"""
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json() is None


def test_logout_clears_cookie(client, admin):
    _login(client)
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204
    assert client.get("/api/auth/me").json() is None


def test_logout_works_without_login(client):
    """语义是「清掉浏览器上的 cookie」，没登录时同样应该成功。"""
    assert client.post("/api/auth/logout").status_code == 204


# ---- 注册（刻意不存在）----


def test_no_register_endpoint_exists(client):
    """需求明确要求点「注册」直接弹「当前网站未开放注册」，那是一句前端
    文案。留一个后端端点等于凭空多一个攻击面，而且早晚会有人忘了它是
    关着的。这条钉住「它不存在」。"""
    resp = client.post(
        "/api/auth/register",
        json={"username": "x", "email": "x@example.com", "password": "12345678"},
    )
    assert resp.status_code == 404
