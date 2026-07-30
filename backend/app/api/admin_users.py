"""Admin 面板的用户管理、白名单管理与系统状态。

Azure 凭证那部分仍在 admin.py，不搬——它自成一体（凭证加密 + 五步自检），
和账号管理没有共享状态，合成一个文件只会让两边都更难读。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.auth import to_dto
from app.api.deps import require_admin
from app.config import settings
from app.db import get_session
from app.errors import UserNotFound, ValidationError
from app.models import AllowedOrigin, BlockedOrigin, Task, User
from app.schemas import (
    AllowedOriginDto,
    CreateAllowedOriginRequest,
    CreateUserRequest,
    SetUserPasswordRequest,
    SetUserStatusRequest,
    SystemStatsDto,
    UserDto,
)
from app.services import origin_rules, users

router = APIRouter(prefix="/api/admin", tags=["admin"])

_ERR = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}}
_ADMIN_ERRORS = {
    401: {**_ERR, "description": "AUTH_REQUIRED"},
    403: {**_ERR, "description": "FORBIDDEN"},
}


def _validated_rule(raw: str) -> str:
    """校验规则语法并归一。语法错误当场拒绝——宁可让人改一次，也不要让
    他配了一条永不生效的规则还以为配好了。"""
    try:
        return origin_rules.parse_rule(raw).raw.lower()
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


# ---- 用户管理 ----


@router.get("/users", response_model=list[UserDto], responses=_ADMIN_ERRORS)
def list_users(
    _: User = Depends(require_admin), db: Session = Depends(get_session)
) -> list[UserDto]:
    return [to_dto(u) for u in users.list_all(db)]


@router.post(
    "/users",
    response_model=UserDto,
    status_code=201,
    responses={**_ADMIN_ERRORS, 409: {**_ERR, "description": "USERNAME_TAKEN"}},
)
def create_user(
    payload: CreateUserRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> UserDto:
    user = users.create(
        db,
        username=payload.username,
        email=payload.email,
        password=payload.password,
        role=payload.role,
    )
    return to_dto(user)


@router.put(
    "/users/{user_id}/status",
    response_model=UserDto,
    responses={**_ADMIN_ERRORS, 404: {**_ERR, "description": "USER_NOT_FOUND"}},
)
def set_user_status(
    user_id: str,
    payload: SetUserStatusRequest,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> UserDto:
    return to_dto(users.set_status(db, user_id, payload.status, actor_id=actor.user_id))


@router.put(
    "/users/{user_id}/password",
    response_model=UserDto,
    responses={**_ADMIN_ERRORS, 404: {**_ERR, "description": "USER_NOT_FOUND"}},
)
def set_user_password(
    user_id: str,
    payload: SetUserPasswordRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> UserDto:
    return to_dto(users.set_password(db, user_id, payload.password))


@router.delete(
    "/users/{user_id}",
    status_code=204,
    responses={**_ADMIN_ERRORS, 404: {**_ERR, "description": "USER_NOT_FOUND"}},
)
def delete_user(
    user_id: str,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> None:
    users.delete(db, user_id, actor_id=actor.user_id)


# ---- 访问白名单（六期先建骨架，默认不启用）----


@router.get("/origins", response_model=list[AllowedOriginDto], responses=_ADMIN_ERRORS)
def list_origins(
    _: User = Depends(require_admin), db: Session = Depends(get_session)
) -> list[AllowedOriginDto]:
    rows = db.query(AllowedOrigin).order_by(AllowedOrigin.created_at.asc()).all()
    return [
        AllowedOriginDto(
            origin_id=r.origin_id, origin=r.origin, note=r.note, created_at=r.created_at
        )
        for r in rows
    ]


@router.post(
    "/origins",
    response_model=AllowedOriginDto,
    status_code=201,
    responses={**_ADMIN_ERRORS, 409: {**_ERR, "description": "已存在"}},
)
def create_origin(
    payload: CreateAllowedOriginRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> AllowedOriginDto:
    origin = _validated_rule(payload.origin)
    exists = db.query(AllowedOrigin).filter(AllowedOrigin.origin == origin).one_or_none()
    if exists is not None:
        raise ValidationError(f"{origin} 已在白名单中")

    row = AllowedOrigin(origin_id=str(uuid.uuid4()), origin=origin, note=payload.note)
    db.add(row)
    db.commit()
    return AllowedOriginDto(
        origin_id=row.origin_id, origin=row.origin, note=row.note, created_at=row.created_at
    )


@router.delete("/origins/{origin_id}", status_code=204, responses=_ADMIN_ERRORS)
def delete_origin(
    origin_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> None:
    row = db.get(AllowedOrigin, origin_id)
    if row is None:
        raise UserNotFound(f"白名单条目 {origin_id} 不存在")
    db.delete(row)
    db.commit()


# ---- 黑名单 ----


@router.get("/blocked", response_model=list[AllowedOriginDto], responses=_ADMIN_ERRORS)
def list_blocked(
    _: User = Depends(require_admin), db: Session = Depends(get_session)
) -> list[AllowedOriginDto]:
    rows = db.query(BlockedOrigin).order_by(BlockedOrigin.created_at.asc()).all()
    return [
        AllowedOriginDto(
            origin_id=r.origin_id, origin=r.origin, note=r.note, created_at=r.created_at
        )
        for r in rows
    ]


@router.post(
    "/blocked",
    response_model=AllowedOriginDto,
    status_code=201,
    responses=_ADMIN_ERRORS,
)
def create_blocked(
    payload: CreateAllowedOriginRequest,
    _: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> AllowedOriginDto:
    origin = _validated_rule(payload.origin)
    exists = db.query(BlockedOrigin).filter(BlockedOrigin.origin == origin).one_or_none()
    if exists is not None:
        raise ValidationError(f"{origin} 已在黑名单中")

    row = BlockedOrigin(origin_id=str(uuid.uuid4()), origin=origin, note=payload.note)
    db.add(row)
    db.commit()
    return AllowedOriginDto(
        origin_id=row.origin_id, origin=row.origin, note=row.note, created_at=row.created_at
    )


@router.delete("/blocked/{origin_id}", status_code=204, responses=_ADMIN_ERRORS)
def delete_blocked(
    origin_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> None:
    row = db.get(BlockedOrigin, origin_id)
    if row is None:
        raise UserNotFound(f"黑名单条目 {origin_id} 不存在")
    db.delete(row)
    db.commit()


# ---- 系统状态 ----


def _dir_bytes(path) -> int:
    """目录占用。单个文件读不到（并发删除、权限）时跳过而不是整体失败——
    这是个只读的观察面板，不该因为一个瞬时消失的临时文件就整页报错。"""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


@router.get("/stats", response_model=SystemStatsDto, responses=_ADMIN_ERRORS)
def system_stats(
    _: User = Depends(require_admin), db: Session = Depends(get_session)
) -> SystemStatsDto:
    by_status: dict[str, int] = {}
    for status, count in (
        db.query(Task.status, func.count(Task.task_id))
        .group_by(Task.status)
        .all()
    ):
        by_status[status] = count

    return SystemStatsDto(
        tasks_total=sum(by_status.values()),
        tasks_by_status=by_status,
        users_total=db.query(User).count(),
        storage_originals_bytes=_dir_bytes(settings.originals_dir),
        storage_outputs_bytes=_dir_bytes(settings.outputs_dir),
        storage_shards_bytes=_dir_bytes(settings.shards_dir),
    )
