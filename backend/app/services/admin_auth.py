"""管理入口的口令校验与会话签发。

只回答「这个请求是不是管理员」，不认识 Azure、不认识凭证。
"""

import binascii
import hashlib
import hmac
import os
import time

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.errors import AdminBadPassword, AdminNotConfigured, AdminUnauthorized

SESSION_COOKIE_NAME = "pptx2pdf_admin"

# scrypt 参数。n=16384 在普通机器上约 100ms，足以让在线爆破无意义，
# 又不至于让每次登录明显变慢。
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

# 口令错误时的固定延迟。自用场景不做账户锁定——锁定的唯一效果
# 是把自己锁在门外。
_WRONG_PASSWORD_DELAY_S = 1.0


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """返回 scrypt:<salt_hex>:<hash_hex>。salt 参数仅供测试构造确定值。

    分隔符用 `:` 而不是 `$`：十六进制段以 a-f 开头时，Docker Compose 会把
    `$<hex>` 当成未定义的变量插值，整段被替换成空串（以 0-9 开头的段
    则幸存，概率相关，约 61% 的哈希会在部署时被吃掉一段或两段）。这只在
    `docker compose`（唯一部署方式）里发生，pydantic-settings 直读 `.env`
    不做插值，本地裸跑测试完全复现不了。`:` 对 Compose 插值免疫，不依赖
    任何人记住要把命令输出里的 `$` 手工转义成 `$$`。
    """
    if salt is None:
        salt = os.urandom(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt:{binascii.hexlify(salt).decode()}:{binascii.hexlify(digest).decode()}"


def verify_password(password: str) -> None:
    """口令正确则静默返回，否则抛异常。

    哈希缺失与哈希格式非法都归一成 AdminNotConfigured——两者对使用者
    是同一件事（管理入口没配好），而区分它们只会泄露配置细节。
    """
    stored = settings.admin_password_hash
    if not stored:
        raise AdminNotConfigured("未配置 PPTX2PDF_ADMIN_PASSWORD_HASH，管理入口不可用")

    parts = stored.split(":")
    if len(parts) != 3 or parts[0] != "scrypt":
        raise AdminNotConfigured("PPTX2PDF_ADMIN_PASSWORD_HASH 格式非法")
    try:
        salt = bytes.fromhex(parts[1])
        expected = bytes.fromhex(parts[2])
    except ValueError as exc:
        raise AdminNotConfigured("PPTX2PDF_ADMIN_PASSWORD_HASH 不是合法的十六进制") from exc

    actual = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    if not hmac.compare_digest(actual, expected):
        time.sleep(_WRONG_PASSWORD_DELAY_S)
        raise AdminBadPassword("口令错误")


def _fernet() -> Fernet:
    """会话签名与 client_secret 加密共用 PPTX2PDF_SECRET_KEY，不派生子密钥。

    密钥用途分离是通行做法，但在本威胁模型下收益为零：能读到
    SECRET_KEY 的攻击者已可直接解密 client_secret，伪造 session 是
    更绕的路径。这是刻意判断，不是疏忽。
    """
    key = settings.secret_key
    if not key:
        raise AdminNotConfigured("未配置 PPTX2PDF_SECRET_KEY，无法签发会话")
    try:
        return Fernet(key.encode())
    except (ValueError, binascii.Error) as exc:
        raise AdminNotConfigured("PPTX2PDF_SECRET_KEY 格式非法") from exc


_SESSION_PAYLOAD = b"admin"


def issue_session() -> str:
    """签发会话 token。Fernet 自带时间戳，过期校验交给 decrypt 的 ttl。"""
    return _fernet().encrypt(_SESSION_PAYLOAD).decode()


def verify_session(token: str | None) -> None:
    """token 有效则静默返回，否则抛 AdminUnauthorized。

    只验证「Fernet 能解开 + 未过期」不够：client_secret 用同一把
    SECRET_KEY 加密（见 _fernet 的注释），如果不校验解出来的明文，
    数据库里 client_secret_encrypted 那段密文本身就是一张有效的管理员
    cookie——原样贴进 cookie 就能登进管理台，且这条路径不需要拿到
    SECRET_KEY，只需要读到数据库（卷快照、备份等更容易暴露的面）。
    这里显式比对明文是不是本模块签发时用的固定 payload，堵住这条路。
    """
    if not token:
        raise AdminUnauthorized("未登录")
    ttl = settings.admin_session_days * 86400
    try:
        payload = _fernet().decrypt(token.encode(), ttl=ttl)
    except InvalidToken as exc:
        raise AdminUnauthorized("会话无效或已过期") from exc
    if payload != _SESSION_PAYLOAD:
        raise AdminUnauthorized("会话内容非法")
