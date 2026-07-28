"""管理入口的四个端点。

把 admin_auth（鉴权）与 graph_selftest（凭证验证）接起来，
并持有「先测后存」这条规则。
"""

from fastapi import APIRouter, Cookie, Depends, Response

from app.config import settings
from app.schemas import AdminLoginRequest
from app.services import admin_auth

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _set_session_cookie(response: Response, token: str) -> None:
    """HttpOnly 与 SameSite 无条件正确；Secure 必须跟配置走——
    HTTP 部署下写死 Secure 会让浏览器不回传 cookie。"""
    response.set_cookie(
        key=admin_auth.SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.admin_session_days * 86400,
        httponly=True,
        samesite="Strict",
        secure=settings.admin_cookie_secure,
        path="/",
    )


def require_admin(
    response: Response,
    session: str | None = Cookie(default=None, alias=admin_auth.SESSION_COOKIE_NAME),
) -> None:
    """鉴权依赖。通过后立刻重新签发 cookie——滑动刷新，活跃使用不掉线。"""
    admin_auth.verify_session(session)
    _set_session_cookie(response, admin_auth.issue_session())


@router.post("/login", status_code=204)
def login(payload: AdminLoginRequest, response: Response) -> Response:
    admin_auth.verify_password(payload.password)
    _set_session_cookie(response, admin_auth.issue_session())
    response.status_code = 204
    return response


@router.post("/logout", status_code=204)
def logout(response: Response) -> Response:
    """不要求鉴权：语义是清掉浏览器上的 cookie，cookie 已过期时同样应该成功。
    若要求鉴权，过期后点登出会得到 401，而用户想做的恰恰是清理这个失效状态。"""
    response.delete_cookie(
        key=admin_auth.SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="Strict",
        secure=settings.admin_cookie_secure,
    )
    response.status_code = 204
    return response
