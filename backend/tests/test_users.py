"""用户服务层：CRUD、唯一性、自锁防护与 admin 引导。

这里的几条不变量都是「一旦破了就把自己锁在门外」那一类，没有第二个
恢复入口（除非重建数据库），所以每条都要有测试守着。
"""

import pytest
from cryptography.fernet import Fernet

from app.errors import UserNotFound, UsernameTaken, ValidationError
from app.services import auth, users

PASSWORD = "hunter2!"


@pytest.fixture(autouse=True)
def _fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "_WRONG_PASSWORD_DELAY_S", 0.0)


@pytest.fixture
def db(tmp_path):
    import app.db as db_module

    return db_module.SessionLocal()


def _mk(db, username="alice", role="user", password=PASSWORD):
    return users.create(
        db, username=username, email=f"{username}@example.com", password=password, role=role
    )


# ---- 唯一性 ----


def test_username_must_be_unique(db):
    _mk(db)
    with pytest.raises(UsernameTaken):
        _mk(db)


def test_username_uniqueness_is_case_insensitive(db):
    """Admin 与 admin 在人眼里是同一个账号。不做大小写归一的话，
    「用户名唯一」这条保证对用户来说就是假的。"""
    _mk(db, username="Alice")
    with pytest.raises(UsernameTaken):
        _mk(db, username="ALICE")


def test_username_is_stored_normalized(db):
    assert _mk(db, username="AlIcE").username == "alice"


def test_email_is_not_unique(db):
    """邮箱刻意不唯一——它只是联系方式，不参与鉴权。强制唯一会挡住
    「admin 给同一个人开测试号」这种正常用法。"""
    users.create(db, username="acc1", email="same@example.com", password=PASSWORD)
    users.create(db, username="acc2", email="same@example.com", password=PASSWORD)
    assert len(users.list_all(db)) == 2


# ---- 校验 ----


@pytest.mark.parametrize("bad", ["ab", "x" * 33, "has space", "há", "a@b"])
def test_rejects_bad_username(db, bad):
    with pytest.raises(ValidationError):
        users.create(db, username=bad, email="e@example.com", password=PASSWORD)


@pytest.mark.parametrize("bad", ["nope", "", "x" * 260 + "@e.com"])
def test_rejects_bad_email(db, bad):
    with pytest.raises(ValidationError):
        users.create(db, username="bob", email=bad, password=PASSWORD)


def test_rejects_short_password(db):
    with pytest.raises(ValidationError):
        users.create(db, username="bob", email="b@example.com", password="short")


def test_rejects_unknown_role(db):
    with pytest.raises(ValidationError):
        users.create(db, username="bob", email="b@example.com", password=PASSWORD, role="root")


# ---- 认证 ----


def test_authenticate_accepts_correct(db):
    u = _mk(db)
    assert users.authenticate(db, "alice", PASSWORD).user_id == u.user_id


def test_authenticate_rejects_wrong_password(db):
    _mk(db)
    assert users.authenticate(db, "alice", "nope") is None


def test_authenticate_rejects_unknown_user(db):
    assert users.authenticate(db, "ghost", PASSWORD) is None


def test_authenticate_rejects_suspended(db):
    u = _mk(db)
    users.set_status(db, u.user_id, "suspended", actor_id="other")
    assert users.authenticate(db, "alice", PASSWORD) is None


def test_password_is_not_stored_in_clear(db):
    u = _mk(db)
    assert PASSWORD not in u.password_hash
    assert u.password_hash.startswith("scrypt:")


# ---- 自锁防护 ----


def test_cannot_suspend_self(db):
    """自用系统没有第二个恢复入口，把自己停掉就等于把自己锁在门外。"""
    admin = _mk(db, username="admin", role="admin")
    with pytest.raises(ValidationError, match="不能暂停自己"):
        users.set_status(db, admin.user_id, "suspended", actor_id=admin.user_id)


def test_can_suspend_others(db):
    admin = _mk(db, username="admin", role="admin")
    other = _mk(db, username="bob")
    assert users.set_status(db, other.user_id, "suspended", actor_id=admin.user_id).status == "suspended"


def test_cannot_delete_self(db):
    admin = _mk(db, username="admin", role="admin")
    with pytest.raises(ValidationError, match="不能删除自己"):
        users.delete(db, admin.user_id, actor_id=admin.user_id)


def test_cannot_delete_last_admin(db):
    """删掉最后一个 admin 之后没有任何人能进管理台，只能重建数据库。"""
    admin = _mk(db, username="admin", role="admin")
    other = _mk(db, username="bob")
    with pytest.raises(ValidationError, match="最后一个管理员"):
        users.delete(db, admin.user_id, actor_id=other.user_id)


def test_can_delete_admin_when_another_exists(db):
    a1 = _mk(db, username="admin", role="admin")
    a2 = _mk(db, username="admin2", role="admin")
    users.delete(db, a1.user_id, actor_id=a2.user_id)
    assert users.get_by_username(db, "admin") is None


def test_delete_unknown_raises(db):
    with pytest.raises(UserNotFound):
        users.delete(db, "no-such-id", actor_id="whoever")


# ---- 改密码 ----


def test_set_password_changes_login(db):
    u = _mk(db)
    users.set_password(db, u.user_id, "brand-new-pass")
    assert users.authenticate(db, "alice", PASSWORD) is None
    assert users.authenticate(db, "alice", "brand-new-pass") is not None


def test_set_password_rejects_short(db):
    u = _mk(db)
    with pytest.raises(ValidationError):
        users.set_password(db, u.user_id, "short")


# ---- admin 引导 ----


def test_bootstrap_creates_admin_from_env_hash(db, monkeypatch):
    monkeypatch.setattr(users.settings, "admin_password_hash", auth.hash_password(PASSWORD))
    monkeypatch.setattr(users.settings, "admin_email", "boot@example.com")

    created = users.bootstrap_admin(db)

    assert created is not None
    assert created.username == "admin"
    assert created.role == "admin"
    assert created.email == "boot@example.com"
    assert users.authenticate(db, "admin", PASSWORD) is not None


def test_bootstrap_does_nothing_without_env_hash(db, monkeypatch):
    """延续四期铁律「不设默认值兜底」：没配就不创建，管理入口继续 503。
    绝不因为「方便」而生成一个默认密码——那等于把门开着还挂个牌子说锁了。
    """
    monkeypatch.setattr(users.settings, "admin_password_hash", None)
    assert users.bootstrap_admin(db) is None
    assert users.list_all(db) == []


def test_bootstrap_is_idempotent(db, monkeypatch):
    """每次启动都会调它，已经有 admin 时必须什么都不做——否则重启会把
    面板里改过的密码冲回环境变量那个值。"""
    monkeypatch.setattr(users.settings, "admin_password_hash", auth.hash_password(PASSWORD))
    users.bootstrap_admin(db)
    users.set_password(db, users.get_by_username(db, "admin").user_id, "changed-in-panel")

    assert users.bootstrap_admin(db) is None
    assert users.authenticate(db, "admin", "changed-in-panel") is not None


def test_bootstrap_refuses_when_username_taken_by_non_admin(db, monkeypatch):
    """用户名被一个普通账号占了。不静默改它的角色——那等于凭环境变量
    给任意账号提权。"""
    _mk(db, username="admin", role="user")
    monkeypatch.setattr(users.settings, "admin_password_hash", auth.hash_password(PASSWORD))

    assert users.bootstrap_admin(db) is None
    assert users.get_by_username(db, "admin").role == "user"
