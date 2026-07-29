"""用户的增删改查与 admin 账号引导。

只做数据操作与不变量守卫，不碰 HTTP、不碰 cookie——那些在 api 层。
"""

import logging
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import (
    UserNotFound,
    UsernameTaken,
    ValidationError,
)
from app.models import User
from app.services import auth

logger = logging.getLogger(__name__)

ADMIN_USERNAME = "admin"
ROLES = ("admin", "user")
STATUSES = ("active", "suspended")

_USERNAME_MIN = 3
_USERNAME_MAX = 32
_PASSWORD_MIN = 8


def normalize_username(raw: str) -> str:
    """用户名统一小写后比对唯一性。

    否则 `Admin` 与 `admin` 会是两个账号，而它们在人眼里是同一个——
    这正是「用户名唯一」要防的事。
    """
    return raw.strip().lower()


def validate_new_user(username: str, email: str, password: str) -> None:
    if not (_USERNAME_MIN <= len(username) <= _USERNAME_MAX):
        raise ValidationError(f"用户名长度需在 {_USERNAME_MIN}-{_USERNAME_MAX} 之间")
    if not username.replace("_", "").replace("-", "").isalnum():
        raise ValidationError("用户名只能包含字母、数字、下划线和连字符")
    if "@" not in email or len(email) > 254:
        raise ValidationError("邮箱格式不合法")
    if len(password) < _PASSWORD_MIN:
        raise ValidationError(f"密码至少 {_PASSWORD_MIN} 位")


def get_by_username(session: Session, username: str) -> User | None:
    return (
        session.query(User)
        .filter(func.lower(User.username) == normalize_username(username))
        .one_or_none()
    )


def get(session: Session, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise UserNotFound(f"用户 {user_id} 不存在")
    return user


def list_all(session: Session) -> list[User]:
    return session.query(User).order_by(User.created_at.asc()).all()


def create(
    session: Session,
    *,
    username: str,
    email: str,
    password: str,
    role: str = "user",
) -> User:
    username = normalize_username(username)
    validate_new_user(username, email, password)
    if role not in ROLES:
        raise ValidationError(f"role 只能是 {ROLES} 之一")
    if get_by_username(session, username) is not None:
        raise UsernameTaken(f"用户名 {username} 已被占用")

    user = User(
        user_id=str(uuid.uuid4()),
        username=username,
        email=email.strip(),
        password_hash=auth.hash_password(password),
        role=role,
        status="active",
    )
    session.add(user)
    session.commit()
    logger.info("user created id=%s username=%s role=%s", user.user_id, username, role)
    return user


def set_status(session: Session, user_id: str, status: str, *, actor_id: str) -> User:
    if status not in STATUSES:
        raise ValidationError(f"status 只能是 {STATUSES} 之一")
    user = get(session, user_id)
    if user.user_id == actor_id and status != "active":
        # 自用系统没有第二个恢复入口，把自己停掉就等于把自己锁在门外
        raise ValidationError("不能暂停自己的账号")
    user.status = status
    session.commit()
    logger.info("user status changed id=%s -> %s", user_id, status)
    return user


def set_password(session: Session, user_id: str, password: str) -> User:
    if len(password) < _PASSWORD_MIN:
        raise ValidationError(f"密码至少 {_PASSWORD_MIN} 位")
    user = get(session, user_id)
    user.password_hash = auth.hash_password(password)
    session.commit()
    logger.info("user password changed id=%s", user_id)
    return user


def delete(session: Session, user_id: str, *, actor_id: str) -> None:
    user = get(session, user_id)
    if user.user_id == actor_id:
        raise ValidationError("不能删除自己的账号")
    if user.role == "admin" and _count_admins(session) <= 1:
        # 删掉最后一个 admin 之后没有任何人能进管理台，只能重建数据库
        raise ValidationError("不能删除最后一个管理员")
    session.delete(user)
    session.commit()
    logger.info("user deleted id=%s username=%s", user_id, user.username)


def _count_admins(session: Session) -> int:
    return session.query(User).filter(User.role == "admin").count()


def authenticate(session: Session, username: str, password: str) -> User | None:
    """凭用户名口令换用户。任何失败都返回 None，不区分原因。

    区分「用户不存在」与「密码错误」会把用户名枚举送给攻击者。
    两种情况都走同一条固定延迟，时序上也不泄露。
    """
    user = get_by_username(session, username)
    if user is None or not auth.password_matches(password, user.password_hash):
        auth.delay_after_bad_password()
        return None
    if user.status != "active":
        auth.delay_after_bad_password()
        return None
    return user


def bootstrap_admin(session: Session) -> User | None:
    """数据库里没有 admin 时，用环境变量里的哈希创建一个。

    延续四期铁律「不设默认值兜底」：没配 PPTX2PDF_ADMIN_PASSWORD_HASH
    就**不创建**，管理入口继续 503。绝不因为「方便」而生成一个默认密码——
    那等于把门开着还挂个牌子说门锁了。

    引导之后环境变量就不再是真相源：admin 在面板里改了密码，改的是库里
    这一行。否则每次重启都会把改过的密码冲回环境变量那个值。
    """
    if _count_admins(session) > 0:
        return None
    stored = settings.admin_password_hash
    if not stored:
        logger.warning("未配置 PPTX2PDF_ADMIN_PASSWORD_HASH，跳过 admin 引导")
        return None

    existing = get_by_username(session, ADMIN_USERNAME)
    if existing is not None:
        # 用户名被一个非 admin 账号占了。不静默改它的角色——那等于凭
        # 环境变量给任意账号提权。
        logger.error("用户名 %s 已被非管理员账号占用，admin 引导中止", ADMIN_USERNAME)
        return None

    user = User(
        user_id=str(uuid.uuid4()),
        username=ADMIN_USERNAME,
        email=settings.admin_email,
        password_hash=stored,
        role="admin",
        status="active",
    )
    session.add(user)
    session.commit()
    logger.info("admin 账号已引导创建 id=%s", user.user_id)
    return user
