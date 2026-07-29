import time

import pytest
from cryptography.fernet import Fernet

from app.errors import AdminBadPassword, AdminNotConfigured, AdminUnauthorized
from app.services import admin_auth


@pytest.fixture(autouse=True)
def _configure_secret_key(monkeypatch):
    """conftest.py 没有为 secret_key 提供全局测试值（各测试文件各自设置）。
    admin_auth 的会话签发/校验依赖 settings.secret_key 才能构造 Fernet，
    这里统一给一个测试值，避免每个用例都要单独 monkeypatch。"""
    monkeypatch.setattr(admin_auth.settings, "secret_key", Fernet.generate_key().decode())


def _configure(monkeypatch, password: str = "hunter2") -> None:
    monkeypatch.setattr(
        admin_auth.settings, "admin_password_hash", admin_auth.hash_password(password)
    )


def test_hash_format():
    h = admin_auth.hash_password("hunter2")
    parts = h.split(":")
    assert parts[0] == "scrypt"
    assert len(parts) == 3
    assert len(bytes.fromhex(parts[1])) == 16
    assert len(bytes.fromhex(parts[2])) == 32
    # 分隔符必须是 ":"，不能是 "$"——Docker Compose 会把 "$<hex>" 当成
    # 未定义变量插值掉，这是 C1 的根因，见 admin_auth.hash_password docstring
    assert "$" not in h


def test_hash_is_salted():
    assert admin_auth.hash_password("hunter2") != admin_auth.hash_password("hunter2")


def test_verify_password_accepts_correct(monkeypatch):
    _configure(monkeypatch)
    admin_auth.verify_password("hunter2")


def test_verify_password_rejects_wrong(monkeypatch):
    _configure(monkeypatch)
    with pytest.raises(AdminBadPassword):
        admin_auth.verify_password("wrong")


def test_verify_password_raises_when_not_configured(monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_password_hash", None)
    with pytest.raises(AdminNotConfigured):
        admin_auth.verify_password("hunter2")


def test_verify_password_raises_on_malformed_hash(monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_password_hash", "not-a-hash")
    with pytest.raises(AdminNotConfigured):
        admin_auth.verify_password("hunter2")


def test_session_roundtrip(monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_session_days", 3)
    token = admin_auth.issue_session()
    admin_auth.verify_session(token)


def test_verify_session_rejects_none():
    with pytest.raises(AdminUnauthorized):
        admin_auth.verify_session(None)


def test_verify_session_rejects_garbage():
    with pytest.raises(AdminUnauthorized):
        admin_auth.verify_session("not-a-token")


def test_verify_session_rejects_same_key_different_plaintext():
    # client_secret 用同一把 SECRET_KEY 加密（admin_auth._fernet 的注释里
    # 说明了这个刻意判断）。如果 verify_session 只看「Fernet 能解开」，
    # 库里的 client_secret_encrypted 密文原样贴进 cookie 就能冒充管理员
    # 会话——不需要拿到 SECRET_KEY，只需要读到数据库。这里构造一个
    # 同密钥、但明文不是 admin_auth 内部固定 payload 的 token，断言必须
    # 被拒绝。
    forged = admin_auth._fernet().encrypt(b"something-else").decode()
    with pytest.raises(AdminUnauthorized):
        admin_auth.verify_session(forged)


def test_verify_session_rejects_expired(monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_session_days", 3)
    token = admin_auth.issue_session()
    # Fernet 的 TTL 以 token 内嵌时间戳为准，把「现在」往后拨 4 天
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 4 * 86400)
    with pytest.raises(AdminUnauthorized):
        admin_auth.verify_session(token)
