import logging
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import GraphNotConfigured
from app.models import GraphCredential

logger = logging.getLogger(__name__)

CREDENTIAL_ROW_ID = 1


@dataclass(frozen=True)
class GraphCredentialData:
    tenant_id: str
    client_id: str
    client_secret: str
    site_id: str
    drive_path: str


def _fernet() -> Fernet:
    """取加密器。密钥缺失或格式非法都归一成 GraphNotConfigured——
    这是「引擎没配好」而不是「服务器内部错误」，用户看到的提示应该
    指向配置动作。"""
    if not settings.secret_key:
        raise GraphNotConfigured(
            "未配置 PPTX2PDF_SECRET_KEY，Graph 引擎不可用。"
            "生成方式见 .env.example。"
        )
    try:
        return Fernet(settings.secret_key.encode())
    except (ValueError, TypeError) as exc:
        raise GraphNotConfigured(
            f"PPTX2PDF_SECRET_KEY 不是合法的 Fernet 密钥: {exc}"
        ) from exc


def load_credentials(session: Session) -> GraphCredentialData:
    fernet = _fernet()
    row = session.get(GraphCredential, CREDENTIAL_ROW_ID)
    if row is None:
        raise GraphNotConfigured(
            "尚未配置 Azure 凭证，请在管理页面填写租户与 SharePoint 站点信息"
        )
    try:
        secret = fernet.decrypt(row.client_secret_encrypted.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        # 密钥换过、密文损坏、或数据是用别的密钥加密的。裸 InvalidToken
        # 不是 AppError，会退化成不带错误码的 500。
        logger.error("Graph 凭证解密失败: %s", exc)
        raise GraphNotConfigured(
            "Graph 凭证无法解密——PPTX2PDF_SECRET_KEY 可能已变更。"
            "请在管理页面重新填写 client secret。"
        ) from exc

    return GraphCredentialData(
        tenant_id=row.tenant_id,
        client_id=row.client_id,
        client_secret=secret,
        site_id=row.site_id,
        drive_path=row.drive_path,
    )


def is_graph_configured(session: Session) -> bool:
    """轻量布尔判定，给自动路由（engine_router.select_engine）用：
    Graph 凭证是否已经配置到可用状态，不抛异常。

    直接复用 load_credentials 并吞掉 GraphNotConfigured——不重新实现一遍
    "密钥在不在、行在不在、解密通不通" 这三条判据，避免两套判定标准
    早晚彼此不一致。三期没有凭证写入路径（管理页面是四期的事），所以
    这里在三期部署里恒为 False，auto 分支因此恒选 libreoffice；这正是
    调用方期望的行为——不应该自动选一个当前部署里必然不可用的引擎。
    """
    try:
        load_credentials(session)
        return True
    except GraphNotConfigured:
        return False


def save_credentials(
    session: Session,
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    site_id: str,
    drive_path: str = "pptx2pdf-staging",
) -> None:
    """写入凭证。单行表，重复调用覆盖同一行。

    三期不会调用它——四期的管理页面才是使用方。现在实现好，
    四期直接用，也让加密逻辑在三期就有测试覆盖。
    """
    fernet = _fernet()
    encrypted = fernet.encrypt(client_secret.encode()).decode()

    row = session.get(GraphCredential, CREDENTIAL_ROW_ID)
    if row is None:
        row = GraphCredential(id=CREDENTIAL_ROW_ID)
        session.add(row)
    row.tenant_id = tenant_id
    row.client_id = client_id
    row.client_secret_encrypted = encrypted
    row.site_id = site_id
    row.drive_path = drive_path
    session.commit()
