"""字体管理端点。

字体存 storage 卷下的 fonts/，api 与 worker 共享，写进去两边立刻生效——
fontconfig 发现目录 mtime 变了会自动重扫，不需要 fc-cache，也不需要重启。

不建数据库表，列表每次现扫目录。
"""

import os
import re
import shutil
import time
import uuid
from dataclasses import replace as dataclass_replace
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_admin
from app.config import settings
from app.errors import FontInvalid, FontNotDeletable, FontNotFound, FontTooLarge, FontUploadExpired
from app.models import User
from app.schemas import FontCommitRequest, FontFaceDto, FontFileDto, FontListDto, FontPreflightDto
from app.services.font_store import (
    ALLOWED_SUFFIXES,
    BUILTIN_DIRS,
    MOUNTED_DIR,
    SOURCE_BUILTIN,
    SOURCE_MANAGED,
    SOURCE_MOUNTED,
    WRITABLE_SOURCES,
    FontFile,
    decode_file_id,
    encode_file_id,
    find_conflicts,
    is_duplicate,
    probe,
    safe_filename,
    scan_dir,
)

router = APIRouter(prefix="/api/admin/fonts", tags=["admin"])

#: 单个字体文件上限。参照：msyh.ttc 约 19 MB，Noto CJK 全集约 20 MB。
#: 不沿用 pptx 那个 600MB 的限制——对字体毫无意义。
MAX_FONT_BYTES = 64 * 1024 * 1024

#: preflight 暂存的 TTL。够管理员看完对比表做决定，又不会长期占盘。
FONT_TMP_TTL_SECONDS = 30 * 60


def _scan_all(include_builtin: bool) -> list[FontFile]:
    """扫出全部三个来源。

    builtin 有 300+ 个文件、每个都要跑两次 fc-query，扫一遍是秒级的，
    所以默认不扫——面板上那一组默认是折叠的。
    """
    found = scan_dir(settings.font_dir, SOURCE_MANAGED)
    found += scan_dir(MOUNTED_DIR, SOURCE_MOUNTED)
    if include_builtin:
        for d in BUILTIN_DIRS:
            if not d.is_dir():
                continue
            # 根目录本身也可能直接放字体——只扫子目录会把它们永久漏掉
            for directory in [d, *sorted(p for p in d.rglob("*") if p.is_dir())]:
                # 排除整个手工挂载子树：那批字体已经作为 mounted 扫过了，
                # 再作为 builtin 扫一遍会让用户看到自己放的字体却删不掉
                if directory == MOUNTED_DIR or directory.is_relative_to(MOUNTED_DIR):
                    continue
                found += scan_dir(directory, SOURCE_BUILTIN)
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


def _sweep_tmp() -> None:
    """清掉过期的暂存文件。

    放在 preflight 入口顺手做，不另起后台任务：uploads 的那个回收器绑在
    uploads 表上，而这里没有表，复用不了。
    """
    tmp = settings.font_tmp_dir
    if not tmp.is_dir():
        return
    deadline = time.time() - FONT_TMP_TTL_SECONDS
    for p in tmp.iterdir():
        try:
            if p.stat().st_mtime < deadline:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
        except OSError:
            continue


def _staged_dir(token: str) -> Path:
    """定位暂存目录。

    token 来自客户端，拼路径前必须锁死格式——pathlib 的
    `base / "/etc"`（绝对路径）或 `base / "../../../etc"`（穿越）都会
    逃出 base，而 `os.replace` 紧接着会把逃出去目录里的任意文件当成
    「暂存的字体」搬进字体目录、并删除原文件。schemas.py 的
    FontCommitRequest.token 那层已经用 pattern 卡过一次，这里是第二道：
    防线只有一道时，任何一次 schema 重构都可能悄悄把它拆掉——
    decode_file_id 与消费方各校验一次的模式在 font_store.py 里已经立过
    先例。
    """
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        # 对调用方而言「token 无效」和「token 过期」是同一件事——没必要
        # 给攻击者一个能区分两者的信号。
        raise FontUploadExpired("这次上传已过期，请重新选择文件")
    return settings.font_tmp_dir / token


def _reserve_target(directory: Path, filename: str) -> tuple[Path, str]:
    """在 directory 下排他式占位一个文件名，撞名就按 -2 / -3 递增重试。

    「先看一眼目录算个候选名、再去创建」的写法有 TOCTOU 窗口：并发的
    两次 commit 可能都看到同一个候选名空闲，然后都往里写，后写的会静默
    覆盖先写的。这里用
    `os.O_CREAT | os.O_EXCL` 把「检查是否存在」与「创建」合成一步原子
    操作，堵死这个窗口——占位失败（文件已存在）就换下一个候选名重试，
    占位成功的那一个才是真正属于本次 commit 的文件名。

    返回的文件此时是一个空占位文件，调用方随后要用 os.replace 把真正
    的内容写进去。
    """
    # 唯一调用方传的是已经过 safe_filename 净化的 source_file.name，此刻
    # 不可利用。补这一行是补齐防线，不是修已知漏洞——这条分支已经在
    # 「每一层各校验一次」的模式上栽过两次（decode_file_id 的 file_id、
    # commit 的 token），这里不该是唯一破例的那个。
    filename = safe_filename(filename)
    stem, dot, suffix = filename.rpartition(".")
    if not dot:
        stem, suffix = filename, ""
    candidate = filename
    n = 2
    while True:
        target = directory / candidate
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            candidate = f"{stem}-{n}{dot}{suffix}" if dot else f"{stem}-{n}"
            n += 1
            continue
        os.close(fd)
        return target, candidate


@router.post("/preflight", response_model=FontPreflightDto)
async def preflight(
    file: UploadFile = File(...), _: User = Depends(require_admin)
) -> FontPreflightDto:
    _sweep_tmp()

    name = safe_filename(file.filename or "font")
    if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        raise FontInvalid("只接受 .ttf / .ttc / .otf")

    tmp_dir = settings.font_tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staged = tmp_dir / token
    staged.mkdir()
    dest = staged / name

    # 边写边计数：真正防内存打爆的是 Starlette 在进入本函数之前就已经
    # 用 SpooledTemporaryFile 落盘/落临时文件解析完了 multipart，这个循环
    # 挡的是另一件事——避免超限内容被写进 font_tmp_dir 长期占磁盘，一旦
    # 超限立刻中止清理，而不是等 read() 把整个文件都读完再算总数。
    written = 0
    try:
        with dest.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_FONT_BYTES:
                    raise FontTooLarge(
                        f"字体文件不能超过 {MAX_FONT_BYTES // 1024 // 1024} MB"
                    )
                fh.write(chunk)
        info = probe(dest, SOURCE_MANAGED)
        if info is None:
            raise FontInvalid("这不是可识别的字体文件")
    except Exception:
        # 被拒的上传不能在暂存区留垃圾，也不能留半个文件让 fontconfig 去扫
        shutil.rmtree(staged, ignore_errors=True)
        raise

    # 必须丢进线程池，不能直接同步调用：这里要扫 349 个内置文件、每个跑
    # 两次 fc-query 子进程 + 算 sha256，是秒级的重活。api 容器的 uvicorn
    # 默认单 worker（deploy/api.Dockerfile 没有 --workers），preflight 又
    # 是 async def（要 await file.read()），直接同步跑会冻结整个事件
    # 循环——上传一个字体期间，转换状态轮询、健康检查、v1 同步接口全部
    # 一起卡住。list_fonts/commit 是普通 def，FastAPI 自动丢线程池，没有
    # 这个问题；这里要手动补上。
    existing = await run_in_threadpool(_scan_all, True)
    dup = is_duplicate(info.sha256, existing)
    return FontPreflightDto(
        token=token,
        incoming=_to_dto(info),
        duplicate_of=_to_dto(dup) if dup else None,
        # 完全相同的文件没有歧义，直接告知即可——不必再列一遍候选让管理员
        # 多余地确认一次「要不要替换成一模一样的东西」
        candidates=[] if dup else [_to_dto(c) for c in find_conflicts(info, existing)],
    )


@router.post("", response_model=FontFileDto)
def commit(
    payload: FontCommitRequest, _: User = Depends(require_admin)
) -> FontFileDto:
    staged = _staged_dir(payload.token)
    if not staged.is_dir() or not any(staged.iterdir()):
        raise FontUploadExpired("这次上传已过期，请重新选择文件")
    source_file = next(staged.iterdir())

    # probe 必须放在任何破坏性操作之前：探测的是暂存文件，字节跟落盘后
    # 完全一样，语义不变，但失败时旧文件没删、新文件没写，客户端能干净
    # 地重试。反过来先删旧文件/落盘再 probe，失败时就是「一半替换一半
    # 失败」——replace 掉的旧文件已经不在了，事后 unlink 新文件也回滚
    # 不回旧文件，是 brief 里反复强调要避免的状态。
    info = probe(source_file, SOURCE_MANAGED)
    if info is None:
        raise FontInvalid("这不是可识别的字体文件")

    # 先校验整个 replace 列表再动手：一半替换一半失败是最难收拾的状态
    targets: list[Path] = []
    for fid in payload.replace:
        try:
            src, fname = decode_file_id(fid)
        except ValueError as exc:
            raise FontNotFound(str(exc)) from exc
        if src not in WRITABLE_SOURCES:
            raise FontNotDeletable(
                "手工挂载与镜像内置的字体不能替换，需要在宿主机上处理"
            )
        target = settings.font_dir / safe_filename(fname)
        if not target.is_file():
            raise FontNotFound(f"要替换的字体 {fname} 已不存在")
        targets.append(target)

    settings.font_dir.mkdir(parents=True, exist_ok=True)
    for t in targets:
        t.unlink()

    # 排他式占位而不是先算名字再创建——见 _reserve_target 的说明。
    # 占位成功后 target 是个空文件，os.replace
    # 原子地把真正内容换进去，两端都在同一个 storage 卷下，不会跨设备。
    final_path, final_name = _reserve_target(settings.font_dir, source_file.name)
    os.replace(source_file, final_path)
    shutil.rmtree(staged, ignore_errors=True)

    # 内容跟上面 probe 时完全一致（只是搬了个位置），不需要再跑一次
    # fc-query 去决定成败——只订正可能被 _reserve_target 改过的文件名
    # /file_id，用于构造返回值。
    info = dataclass_replace(
        info, file_id=encode_file_id(SOURCE_MANAGED, final_name), filename=final_name
    )
    return _to_dto(info)
