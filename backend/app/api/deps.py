"""鉴权依赖，三级逐层收紧。

前端把 /admin 入口藏起来、把 Graph 选项置灰，都只是体验；**这里才是
边界**。绕过前端直接打 API 是最基本的渗透手法，所以每个受保护端点都
必须挂上这里的依赖，而不是指望调用方走界面。
"""

from fastapi import Cookie, Depends, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.errors import AdminUnauthorized, AuthRequired, Forbidden
from app.models import User
from app.services import auth

SESSION_COOKIE_NAME = auth.SESSION_COOKIE_NAME


def set_session_cookie(response: Response, token: str) -> None:
    """HttpOnly 与 SameSite 无条件正确；Secure 必须跟配置走——
    HTTP 部署下写死 Secure 会让浏览器不回传 cookie，表现为「登录成功
    但立刻掉线」且极难排查。"""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.admin_session_days * 86400,
        httponly=True,
        samesite="Strict",
        secure=settings.admin_cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="Strict",
        secure=settings.admin_cookie_secure,
        path="/",
    )


def current_user_optional(
    response: Response,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: Session = Depends(get_session),
) -> User | None:
    """有有效会话就返回用户，否则返回 None。任何情况都不抛。

    每次都回库查一次用户状态，而不是只验签名：不查的话，暂停或删除一个
    账号最多要等 3 天（会话有效期）才真正生效，那这两个功能就是假的。
    代价是一次 SQLite 主键查询，可忽略。
    """
    if not session:
        return None
    try:
        user_id = auth.verify_session(session)
    except AdminUnauthorized:
        return None
    user = db.get(User, user_id)
    if user is None or user.status != "active":
        return None
    # 滑动刷新：活跃使用不掉线
    set_session_cookie(response, auth.issue_session(user.user_id))
    return user


def require_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise AuthRequired("请先登录")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise Forbidden("需要管理员权限")
    return user
