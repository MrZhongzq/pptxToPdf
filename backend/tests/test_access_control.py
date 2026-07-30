"""访问控制中间件：黑名单（全站）与来源白名单（只管 v1）。

七期把白名单的作用域从「所有写请求」收窄到「只管 v1」，这推翻了六期的
设计——那时为了防止管理员把自己关在门外，加了「白名单为空时放行」的
保险。现在网页根本不走白名单，那个风险不存在，保险也一并去掉：**空白
名单的语义变成「v1 谁也不许用」**。

这两条语义都要有测试守着，因为它们错的方向都是静默的：白名单错了是
「本该拦的放进来」，黑名单错了是「本该进来的被拦在外面」。
"""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import app
from app.models import AllowedOrigin, BlockedOrigin
from app.services import auth


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


def _block(db, origin: str):
    db.add(BlockedOrigin(origin_id=origin, origin=origin))
    db.commit()


def _allow(db, origin: str):
    db.add(AllowedOrigin(origin_id=origin, origin=origin))
    db.commit()


# ---- 网页不受白名单影响 ----


def test_webui_works_even_with_empty_whitelist(client):
    """需求原话：网页永远不受防跨站影响，即使白名单不允许任何外部访问。

    这条是七期的核心语义变更，破了它整站就会因为「白名单没配」而不可用。
    """
    from app.config import settings

    assert settings.origin_guard_enabled is True  # 七期起默认开启
    resp = client.get("/api/auth/me", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 200


def test_webui_write_request_is_not_checked_against_whitelist(client, db):
    """六期时这会被拦（写请求 + 来源不在白名单）。七期起网页完全不看白名单。"""
    _allow(db, "only-this.example")
    resp = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "whatever"},
        headers={"Origin": "http://evil.example"},
    )
    # 401（凭据错）而不是 403（跨站）——说明根本没走白名单那条路
    assert resp.status_code == 401
    assert resp.json()["code"] == "BAD_CREDENTIALS"


# ---- v1 走白名单 ----


def test_v1_rejected_when_whitelist_is_empty(client):
    """空白名单 = v1 谁也不许用。六期那道「为空则放行」的保险已去掉。"""
    resp = client.get("/v1/convert", params={"fileUrl": "http://example.com/a.pptx"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "CROSS_ORIGIN_BLOCKED"


def test_v1_allowed_when_client_ip_is_whitelisted(client, db):
    """v1 的调用方多半是脚本，不会带 Origin——只看头部等于没有防护，
    所以客户端 IP 必须能作为判定依据。TestClient 的 client.host 是
    testclient。"""
    _allow(db, "testclient")
    resp = client.get("/v1/convert", params={"fileUrl": "http://127.0.0.1/a.pptx"})
    # 过了中间件，倒在 SSRF 防护上（指向环回地址）
    assert resp.status_code == 400
    assert resp.json()["code"] == "URL_NOT_ALLOWED"


def test_v1_allowed_by_origin_header(client, db):
    _allow(db, "good.example")
    resp = client.get(
        "/v1/convert",
        params={"fileUrl": "http://127.0.0.1/a.pptx"},
        headers={"Origin": "http://good.example"},
    )
    assert resp.status_code == 400  # 过了白名单


def test_v1_whitelist_supports_wildcard(client, db):
    _allow(db, "*.example.com")
    resp = client.get(
        "/v1/convert",
        params={"fileUrl": "http://127.0.0.1/a.pptx"},
        headers={"Origin": "http://api.example.com"},
    )
    assert resp.status_code == 400


def test_v1_no_graph_modifier_blocks_graph_engine(client, db):
    """@no_graph 不影响放行，只影响放行之后能不能选 graph。"""
    _allow(db, "testclient||@no_graph")
    resp = client.get(
        "/v1/convert",
        params={"fileUrl": "http://example.com/a.pptx", "engine": "graph"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ENGINE_NOT_ALLOWED"


def test_v1_no_graph_still_allows_libreoffice(client, db):
    _allow(db, "testclient||@no_graph")
    resp = client.get("/v1/convert", params={"fileUrl": "http://127.0.0.1/a.pptx"})
    assert resp.status_code == 400  # 放行了，倒在 SSRF 上


# ---- 黑名单 ----


def test_blacklist_blocks_the_webui_too(client, db):
    """黑名单与白名单的关键区别：它把网页也拦掉。"""
    _block(db, "testclient")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 403
    assert resp.json()["code"] == "ORIGIN_BLOCKED"


def test_blacklist_blocks_v1(client, db):
    _block(db, "testclient")
    resp = client.get("/v1/convert", params={"fileUrl": "http://example.com/a.pptx"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "ORIGIN_BLOCKED"


def test_blacklist_wins_over_whitelist(client, db):
    """需求：黑名单优先级高于防跨站，先匹配到就直接 403 出去。"""
    _allow(db, "testclient")
    _block(db, "testclient")
    resp = client.get("/v1/convert", params={"fileUrl": "http://example.com/a.pptx"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "ORIGIN_BLOCKED"


def test_blacklist_supports_wildcard(client, db):
    _block(db, "*.evil.example")
    resp = client.get("/api/auth/me", headers={"Origin": "http://a.evil.example"})
    assert resp.status_code == 403


def test_blacklist_except_lets_one_subdomain_through(client, db):
    _block(db, "*.evil.example||@except{ok.evil.example}")
    assert (
        client.get("/api/auth/me", headers={"Origin": "http://ok.evil.example"}).status_code
        == 200
    )
    assert (
        client.get("/api/auth/me", headers={"Origin": "http://bad.evil.example"}).status_code
        == 403
    )


def test_empty_blacklist_blocks_nothing(client):
    assert client.get("/api/auth/me").status_code == 200
