"""Azure 凭证的读写端点。

持有「先测后存」这条规则：凭证必须先通过五步自检才允许落库。

六期把鉴权与账号管理搬走了——登录/登出在 api/auth.py，用户与白名单
管理在 api/admin_users.py，鉴权依赖在 api/deps.py。这里只剩凭证本身。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.db import get_session
from app.errors import (
    GraphNotConfigured,
    GraphSelftestFailed,
    ValidationError,
)
from app.models import User
from app.schemas import (
    GraphCredentialsDto,
    GraphCredentialsUpdate,
    SelftestResultDto,
    SelftestStepDto,
)
from app.services import graph_credentials
from app.services.graph_credentials import GraphCredentialData
from app.services.graph_selftest import STEPS, run_selftest

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/graph-credentials", response_model=GraphCredentialsDto)
def get_graph_credentials(
    _: User = Depends(require_admin), db: Session = Depends(get_session)
) -> GraphCredentialsDto:
    try:
        data = graph_credentials.load_credentials(db)
    except GraphNotConfigured:
        return GraphCredentialsDto(
            tenant_id="",
            client_id="",
            site_id="",
            drive_path="pptx2pdf-staging",
            secret_configured=False,
        )
    return GraphCredentialsDto(
        tenant_id=data.tenant_id,
        client_id=data.client_id,
        site_id=data.site_id,
        drive_path=data.drive_path,
        secret_configured=True,
    )


@router.put("/graph-credentials", response_model=SelftestResultDto)
def put_graph_credentials(
    payload: GraphCredentialsUpdate,
    _: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> SelftestResultDto:
    """先测后存：五步自检全绿才写库。

    配错的凭证一旦入库，三期的引擎每次转换都会去读它——期间所有走
    Graph 的任务都会失败，而失败原因（一个 Graph 原始报错）与「你配错
    了」之间隔着数层抽象。这条顺序把那个窗口彻底关闭：run_selftest 在
    save_credentials 之前调用且不捕获它的异常，任何一步——包括
    run_selftest 内部未预期的穿透异常——都会在触达 save_credentials 之前
    终止请求，数据库不会被写入半条记录。
    """
    secret = payload.client_secret
    if not secret:
        # 留空 = 沿用已存的值；库里没有则说明是首次配置，必须填
        try:
            secret = graph_credentials.load_credentials(db).client_secret
        except GraphNotConfigured as exc:
            raise ValidationError("首次配置必须填写 client_secret") from exc

    creds = GraphCredentialData(
        tenant_id=payload.tenant_id,
        client_id=payload.client_id,
        client_secret=secret,
        site_id=payload.site_id,
        drive_path=payload.drive_path,
    )

    results = run_selftest(creds)

    # run_selftest 的 docstring 明确写了：它对"已知失败类别"保证返回列表，
    # 未预期的异常仍会穿透（不在这里裸 except 兜底，理由同 selftest 模块——
    # 不把编程错误伪装成诊断）。但即便它按契约返回了列表，这一层也不能
    # 假定返回值形状必然正确：如果因为未来的重构 results 变成空列表或
    # 缺了某几步，`all(r.ok for r in results)` 在空/残缺输入上会给出
    # 空真值式的误判（空列表的 all() 恒为 True），把"没真正测过"误报成
    # "全绿"而写库。显式校验步骤集合完整，形状不对一律当失败处理。
    if not results or {r.step for r in results} != set(STEPS):
        bad_steps = [SelftestStepDto(step=r.step, ok=r.ok, detail=r.detail) for r in results]
        raise GraphSelftestFailed(
            "自检返回的结果集不完整（步骤缺失或为空），视为未通过", steps=bad_steps
        )

    steps = [SelftestStepDto(step=r.step, ok=r.ok, detail=r.detail) for r in results]
    if not all(r.ok for r in results):
        raise GraphSelftestFailed(steps=steps)

    graph_credentials.save_credentials(
        db,
        tenant_id=creds.tenant_id,
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        site_id=creds.site_id,
        drive_path=creds.drive_path,
    )
    return SelftestResultDto(ok=True, steps=steps)
