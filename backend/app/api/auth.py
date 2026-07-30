"""账号端点：登录、登出、查当前用户。

刻意**没有注册端点**。需求明确要求点「注册」直接弹「当前网站未开放注册」，
那是一句前端文案；为它留一个后端端点等于凭空多一个攻击面，而且早晚会有人
忘了它是关着的。真要开放注册时再加，那时也该配上邮箱验证与限流。
"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import (
    clear_session_cookie,
    current_user_optional,
    set_session_cookie,
)
from app.db import get_session
from app.errors import BadCredentials
from app.models import User
from app.schemas import LoginRequest, UserDto
from app.services import auth, users

router = APIRouter(prefix="/api/auth", tags=["auth"])

_ERR = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}}


def to_dto(user: User) -> UserDto:
    return UserDto(
        user_id=user.user_id,
        username=user.username,
        email=user.email,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
    )


@router.post(
    "/login",
    response_model=UserDto,
    responses={401: {**_ERR, "description": "BAD_CREDENTIALS"}},
)
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_session),
) -> UserDto:
    user = users.authenticate(db, payload.username, payload.password)
    if user is None:
        raise BadCredentials("用户名或密码错误")
    set_session_cookie(response, auth.issue_session(user.user_id))
    return to_dto(user)


@router.post("/logout", status_code=204)
def logout(response: Response) -> Response:
    clear_session_cookie(response)
    response.status_code = 204
    return response


@router.get("/me", response_model=UserDto | None)
def me(user: User | None = Depends(current_user_optional)) -> UserDto | None:
    """未登录返回 null 而不是 401。

    前端在每次进页面时都会打这个端点判断右上角显示「登录」还是用户名，
    未登录是完全正常的状态；用 401 表达会让浏览器控制台常态化地红一片，
    也会诱使前端把「正常的未登录」和「会话过期」写成同一条错误分支。
    """
    return to_dto(user) if user else None
