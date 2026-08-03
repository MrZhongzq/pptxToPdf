"""字体管理端点。

字体存 storage 卷下的 fonts/，api 与 worker 共享，写进去两边立刻生效——
fontconfig 发现目录 mtime 变了会自动重扫，不需要 fc-cache，也不需要重启。

不建数据库表，列表每次现扫目录。
"""

from fastapi import APIRouter, Depends

from app.api.deps import require_admin
from app.config import settings
from app.errors import FontNotDeletable, FontNotFound
from app.models import User
from app.schemas import FontFaceDto, FontFileDto, FontListDto
from app.services.font_store import (
    BUILTIN_DIRS,
    MOUNTED_DIR,
    SOURCE_BUILTIN,
    SOURCE_MANAGED,
    SOURCE_MOUNTED,
    WRITABLE_SOURCES,
    FontFile,
    decode_file_id,
    safe_filename,
    scan_dir,
)

router = APIRouter(prefix="/api/admin/fonts", tags=["admin"])


def _scan_all(include_builtin: bool) -> list[FontFile]:
    """扫出全部三个来源。

    builtin 有 300+ 个文件、每个都要跑两次 fc-query，扫一遍是秒级的，
    所以默认不扫——面板上那一组默认是折叠的。
    """
    found = scan_dir(settings.font_dir, SOURCE_MANAGED)
    found += scan_dir(MOUNTED_DIR, SOURCE_MOUNTED)
    if include_builtin:
        for d in BUILTIN_DIRS:
            for sub in sorted(p for p in d.rglob("*") if p.is_dir()) if d.is_dir() else []:
                if sub == MOUNTED_DIR:
                    continue
                found += scan_dir(sub, SOURCE_BUILTIN)
    return found


def _to_dto(f: FontFile) -> FontFileDto:
    return FontFileDto(
        file_id=f.file_id,
        filename=f.filename,
        source=f.source,
        families=list(f.families),
        faces=[FontFaceDto(family=x.family, style=x.style) for x in f.faces],
        # 一个文件里各 face 的版本通常相同，取第一个即可
        version=f.faces[0].version if f.faces else "-",
        charset_count=f.charset_count,
        size_bytes=f.size_bytes,
        modified_at=f.modified_at,
        deletable=f.deletable,
    )


@router.get("", response_model=FontListDto)
def list_fonts(
    include_builtin: bool = False, _: User = Depends(require_admin)
) -> FontListDto:
    found = _scan_all(include_builtin)
    return FontListDto(
        managed=[_to_dto(f) for f in found if f.source == SOURCE_MANAGED],
        mounted=[_to_dto(f) for f in found if f.source == SOURCE_MOUNTED],
        builtin=[_to_dto(f) for f in found if f.source == SOURCE_BUILTIN],
    )


@router.delete("/{file_id}", status_code=204)
def delete_font(file_id: str, _: User = Depends(require_admin)) -> None:
    try:
        source, filename = decode_file_id(file_id)
    except ValueError as exc:
        raise FontNotFound(str(exc)) from exc

    if source not in WRITABLE_SOURCES:
        raise FontNotDeletable(
            "手工挂载与镜像内置的字体不归面板管理，需要在宿主机上处理"
        )

    # 再过一次 safe_filename：file_id 是客户端传来的，即使 base64 解出来
    # 带路径分隔符也不能让它逃出字体目录
    target = settings.font_dir / safe_filename(filename)
    if not target.is_file():
        raise FontNotFound(f"字体 {filename} 不存在")
    target.unlink()
