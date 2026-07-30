"""Azure 凭证端点：读、写与「先测后存」。

登录/会话相关的用例六期搬到了 test_auth_api.py——那些验的是账号体系，
与凭证本身无关。
"""
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.errors import GraphNotConfigured
from app.main import app
from app.services import auth, graph_credentials, graph_selftest, users
from app.services.graph_selftest import StepResult

PASSWORD = "hunter2!"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_cookie_secure", False)
    # conftest.py 没有为 secret_key 提供全局测试值（各测试文件各自设置，
    # 见 test_auth.py 同名注释）。auth 的会话签发/校验依赖
    # settings.secret_key 才能构造 Fernet，这里补上。
    monkeypatch.setattr(auth.settings, "secret_key", Fernet.generate_key().decode())
    # 口令错误路径的 1 秒延迟在测试里没有意义，抹掉
    monkeypatch.setattr(auth, "_WRONG_PASSWORD_DELAY_S", 0.0)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_user(db_session):
    """六期起管理员是 users 表里的一行，不再是环境变量里的一个口令。"""
    return users.create(
        db_session,
        username="admin",
        email="admin@example.com",
        password=PASSWORD,
        role="admin",
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


_GREEN = [StepResult(s, True, None) for s in graph_selftest.STEPS]


def _stub_selftest(monkeypatch, results):
    calls = []

    def fake(creds, **kwargs):
        calls.append(creds)
        return results

    monkeypatch.setattr(graph_selftest, "run_selftest", fake)
    monkeypatch.setattr("app.api.admin.run_selftest", fake)
    return calls


def test_put_runs_selftest_before_saving(client, admin_session, db_session, monkeypatch):
    calls = _stub_selftest(monkeypatch, _GREEN)
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "t-1", "client_id": "c-1", "client_secret": "s-1",
            "site_id": "site-1", "drive_path": "staging",
        },
    )
    assert resp.status_code == 200
    assert len(calls) == 1, "保存前必须跑自检"
    saved = graph_credentials.load_credentials(db_session)
    assert saved.tenant_id == "t-1"
    assert saved.client_id == "c-1"
    assert saved.client_secret == "s-1"
    assert saved.site_id == "site-1"
    assert saved.drive_path == "staging"
    # 钉死字段映射与加解密回环本身：自检收到的那份凭证必须与事后从库里
    # 读出来的那份逐字段相等（GraphCredentialData 是 frozen dataclass，
    # 可以直接 ==）。只断言一两个字段回环，端点可以把其余字段写错、写反、
    # 写死而所有测试毫无反应——这正是「先测后存」要证明却没证明的那部分：
    # 不是「测了才存」，而是「存的就是测的那份」。
    assert calls[0] == saved


def test_put_does_not_save_when_selftest_fails(client, admin_session, db_session, monkeypatch):
    failing = [
        StepResult("token", True, None),
        StepResult("drive", False, "site_id 写错"),
        StepResult("upload", None, None),
        StepResult("convert", None, None),
        StepResult("delete", None, None),
    ]
    _stub_selftest(monkeypatch, failing)
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "t-1", "client_id": "c-1", "client_secret": "s-1",
            "site_id": "bad", "drive_path": "staging",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "GRAPH_SELFTEST_FAILED"
    assert body["steps"][1]["ok"] is False
    assert body["steps"][2]["ok"] is None
    # 库里一个字节都不许动
    with pytest.raises(GraphNotConfigured):
        graph_credentials.load_credentials(db_session)


def test_put_treats_incomplete_selftest_result_as_failure(
    client, admin_session, db_session, monkeypatch
):
    # run_selftest 的契约保证返回完整的五步列表，但端点不能假定这个形状
    # 永远成立——如果它意外返回了一个缺步骤的列表（这里模拟成只有 "token"
    # 一步、且是 True），`all(r.ok for r in results)` 不会捕捉到这种残缺，
    # 必须靠显式校验步骤集合完整来兜底，否则会把「没真正测过」误判成
    # 「全绿」而写库。
    incomplete = [StepResult("token", True, None)]
    _stub_selftest(monkeypatch, incomplete)
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "t-1", "client_id": "c-1", "client_secret": "s-1",
            "site_id": "site-1", "drive_path": "staging",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "GRAPH_SELFTEST_FAILED"
    with pytest.raises(GraphNotConfigured):
        graph_credentials.load_credentials(db_session)


def test_put_blank_secret_reuses_stored(client, admin_session, db_session, monkeypatch):
    graph_credentials.save_credentials(
        db_session, tenant_id="old-t", client_id="old-c",
        client_secret="STORED-SECRET", site_id="old-s", drive_path="old-d",
    )
    calls = _stub_selftest(monkeypatch, _GREEN)
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "new-t", "client_id": "new-c", "client_secret": "",
            "site_id": "new-s", "drive_path": "new-d",
        },
    )
    assert resp.status_code == 200
    # 自检拿到的必须是库里的旧 secret
    assert calls[0].client_secret == "STORED-SECRET"
    saved = graph_credentials.load_credentials(db_session)
    assert saved.client_secret == "STORED-SECRET"
    assert saved.tenant_id == "new-t"
    assert saved.client_id == "new-c"
    assert saved.site_id == "new-s"
    assert saved.drive_path == "new-d"
    # 同上：自检收到的那份必须与库里的那份逐字段相等
    assert calls[0] == saved


def test_put_blank_secret_rejected_on_first_config(client, admin_session, monkeypatch):
    _stub_selftest(monkeypatch, _GREEN)
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "t", "client_id": "c", "client_secret": "",
            "site_id": "s", "drive_path": "d",
        },
    )
    assert resp.status_code == 422
    assert "client_secret" in resp.text


def test_put_rejects_anonymous(client):
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "t", "client_id": "c", "client_secret": "s",
            "site_id": "s", "drive_path": "d",
        },
    )
    assert resp.status_code == 401
