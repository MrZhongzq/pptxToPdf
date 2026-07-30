"""HTTP v1：一次请求换一份 PDF。

    GET /v1/convert?fileUrl=<url>&engine=libreoffice
                   &user=<用户名>&pwd=<密码>
                   &animations=true&outline=false&margins=false

同步：下载 → 剥离媒体 → （可选）展开动画 → 转换 → 后处理 → **直接回传
PDF 字节**。不返回任务 id，不需要轮询。

## 几个刻意的选择

**参数名用英文小写。** 需求原文举例写的是 `书签大纲=False&重映射=False`，
那是在示意含义。URL 里放中文参数名要经过百分号编码，各家 HTTP 客户端
行为不一，调试时极易踩坑。

**凭据走查询串**是需求指定的形式。它会进服务器访问日志与浏览器历史——
文档里写明了，建议只在受控网络里用。

**LibreOffice 不要求身份，Graph 必须带 user/pwd。** 与 webui 同一条理由：
Graph 消耗租户配额。

**不复用 Task 表。** v1 是一次性的同步调用，没有「查进度」这回事；建一行
任务只会在库里堆下永远不会被人看的记录。代价是 v1 的转换不出现在 admin
的任务统计里，这是刻意的取舍。
"""

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.errors import AppError, AuthRequired, Forbidden, ValidationError
from app.schemas import ConversionOptions
from app.services import users
from app.services.animation_expand import expand_animations
from app.services.engines import get_engine
from app.services.graph_credentials import is_graph_configured
from app.services.media_strip import strip_media
from app.services.postprocess import apply as apply_postprocess
from app.services.pipeline import compute_timeout_s
from app.services.pptx_probe import extract_titles, probe
from app.services.url_fetch import fetch_to

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["v1"])

ENGINES = ("libreoffice", "graph")
DOWNLOAD_TIMEOUT_S = 120.0


class EngineNotAllowed(AppError):
    """来源规则里带了 @no_graph，但请求指定了 graph。"""

    code = "ENGINE_NOT_ALLOWED"
    http_status = 403


@router.get(
    "/convert",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "转换好的 PDF"},
        400: {"description": "参数不合法 / URL 不允许访问"},
        401: {"description": "Graph 通道需要有效凭据"},
        403: {"description": "来源被拒绝，或该来源不允许使用 graph"},
        413: {"description": "文件超过上限"},
        502: {"description": "下载源站失败"},
    },
)
def convert(
    request: Request,
    fileUrl: str = Query(..., description="要转换的 pptx 的公网地址"),  # noqa: N803
    engine: str = Query("libreoffice", description="libreoffice | graph"),
    user: str | None = Query(None, description="用户名，graph 必填"),
    pwd: str | None = Query(None, description="密码，graph 必填"),
    animations: bool = Query(False, description="按 p:timing 展开动画分步"),
    outline: bool = Query(False, description="生成 PDF 书签大纲"),
    margins: bool = Query(False, description="页边距重映射，右侧留白"),
    session: Session = Depends(get_session),
) -> FileResponse:
    if engine not in ENGINES:
        raise ValidationError(f"engine 只能是 {' | '.join(ENGINES)}")

    # 中间件把命中的白名单规则挂在 request.state 上（见 main.access_control_middleware）。
    # @no_graph 不影响放行，只影响放行之后能不能选 graph。
    rule = getattr(request.state, "origin_rule", None)
    if engine == "graph" and rule is not None and rule.no_graph:
        raise EngineNotAllowed("该来源不允许使用 graph 引擎")

    if engine == "graph":
        if not user or not pwd:
            raise AuthRequired("graph 引擎需要提供 user 与 pwd")
        account = users.authenticate(session, user, pwd)
        if account is None:
            raise AuthRequired("用户名或密码错误")
        if not is_graph_configured(session):
            raise Forbidden("服务端尚未配置 Graph 凭证")

    options = ConversionOptions(
        expand_animations=animations, pdf_outline=outline, remap_margins=margins
    )

    workdir = Path(tempfile.mkdtemp(prefix="v1_"))
    src = workdir / "input.pptx"
    try:
        fetched = fetch_to(fileUrl, src, timeout_s=DOWNLOAD_TIMEOUT_S)
        logger.info(
            "v1 downloaded url=%s bytes=%d engine=%s", fileUrl[:120], fetched.size_bytes, engine
        )

        meta = probe(src)
        strip_media(src)
        if options.expand_animations:
            result = expand_animations(src)
            if result.expanded:
                meta = probe(src)
        titles = extract_titles(src) if options.pdf_outline else []

        size_bytes = src.stat().st_size
        dest = workdir / "output.pdf"
        get_engine(engine, session=session).convert(
            src, meta, dest, timeout_s=compute_timeout_s(meta.slide_count, size_bytes)
        )
        apply_postprocess(dest, options, titles)

        stem = Path(fetched.filename).stem or "converted"
        # 产出落到一个独立目录再交给 FileResponse：workdir 会在
        # background 里被删掉，而 FileResponse 是流式的，删早了就传了半截。
        final_dir = Path(tempfile.mkdtemp(prefix="v1_out_"))
        final = final_dir / f"{stem}.pdf"
        shutil.move(str(dest), final)

        return FileResponse(
            str(final),
            media_type="application/pdf",
            filename=f"{stem}.pdf",
            background=_cleanup(final_dir),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _cleanup(path: Path):
    """响应发完之后再删临时目录。

    FileResponse 是流式的：在 return 之前删掉文件，客户端只会收到半截
    甚至空响应。BackgroundTask 在响应体发送完毕后才跑。
    """
    from starlette.background import BackgroundTask

    return BackgroundTask(lambda: shutil.rmtree(path, ignore_errors=True))
