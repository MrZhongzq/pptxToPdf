"""口令哈希与会话签发。

只回答「这个 token 对应哪个用户」，不查数据库、不认识角色、不认识
Azure 凭证——用户是否存在、是否被暂停、是不是 admin，都由调用方
（api/deps.py）拿着 user_id 回库判断。

四期这里叫 admin_auth，只处理单一口令；六期引入账号体系后改名并把
会话载荷从固定的 b"admin" 换成 user_id。
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


def password_matches(password: str, stored: str) -> bool:
    """比对明文与存库的哈希。格式非法一律返回 False。

    与 verify_password 的区别：那个是四期的「对着环境变量里的口令验」，
    这个是「对着某个用户行上的哈希验」。格式非法在这里不抛
    AdminNotConfigured——用户行上的哈希坏掉是数据问题，不是配置问题，
    对调用方而言就是「这个密码不对」，不该把一个用户的坏数据升级成
    整个管理入口 503。
    """
    parts = stored.split(":")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    try:
        salt = bytes.fromhex(parts[1])
        expected = bytes.fromhex(parts[2])
    except ValueError:
        return False
    actual = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return hmac.compare_digest(actual, expected)


def delay_after_bad_password() -> None:
    """口令错误后的固定延迟。自用场景不做账户锁定——锁定的唯一效果
    是把自己锁在门外。"""
    time.sleep(_WRONG_PASSWORD_DELAY_S)


def verify_password(password: str) -> None:
    """口令正确则静默返回，否则抛异常。

    哈希缺失与哈希格式非法都归一成 AdminNotConfigured——两者对使用者
    是同一件事（管理入口没配好），而区分它们只会泄露配置细节。

    六期起这个函数只用于**引导**：数据库里还没有 admin 用户时，拿环境
    变量里的哈希创建它。日常登录走 password_matches。
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


# 会话明文的固定前缀。四期这里是整个 payload（b"admin"），六期要带上
# user_id，于是改成前缀 + user_id。前缀不能去掉，理由见 verify_session。
_SESSION_PREFIX = b"session:v1:"


def issue_session(user_id: str) -> str:
    """签发会话 token。Fernet 自带时间戳，过期校验交给 decrypt 的 ttl。"""
    return _fernet().encrypt(_SESSION_PREFIX + user_id.encode()).decode()


def verify_session(token: str | None) -> str:
    """token 有效则返回其中的 user_id，否则抛 AdminUnauthorized。

    只验证「Fernet 能解开 + 未过期」不够：client_secret 用同一把
    SECRET_KEY 加密（见 _fernet 的注释），如果不校验解出来的明文，
    数据库里 client_secret_encrypted 那段密文本身就是一张有效的管理员
    cookie——原样贴进 cookie 就能登进管理台，且这条路径不需要拿到
    SECRET_KEY，只需要读到数据库（卷快照、备份等更容易暴露的面）。

    六期把载荷从固定值换成了 user_id，这条防护**必须跟着改造而不能
    丢掉**：改成前缀校验。Azure 的 client_secret 是租户生成的随机串，
    不可能以 "session:v1:" 开头，那条路径依然堵着。
    """
    if not token:
        raise AdminUnauthorized("未登录")
    ttl = settings.admin_session_days * 86400
    try:
        payload = _fernet().decrypt(token.encode(), ttl=ttl)
    except InvalidToken as exc:
        raise AdminUnauthorized("会话无效或已过期") from exc
    if not payload.startswith(_SESSION_PREFIX):
        raise AdminUnauthorized("会话内容非法")
    user_id = payload[len(_SESSION_PREFIX):].decode(errors="replace")
    if not user_id:
        raise AdminUnauthorized("会话内容非法")
    return user_id
