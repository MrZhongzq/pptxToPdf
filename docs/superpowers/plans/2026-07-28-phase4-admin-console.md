# 四期实施计划：管理入口与 Azure 凭证配置

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给三期做好但无法配置的 Graph 通道配一把钥匙——一个受口令保护的管理页面，能配置 Azure 凭证并在保存前验证其可用。

**Architecture:** 后端只增不改。新增 `admin_auth`（口令 + 签名 cookie）、`graph_selftest`（五步连通性验证）两个服务和一个 `/api/admin` 路由，接上三期已就绪但零调用方的 `save_credentials`。前端新增 `/admin` 页面，按 `pathname` 分发，不引入路由库。

**Tech Stack:** FastAPI、SQLAlchemy、`hashlib.scrypt`（标准库）、`cryptography.Fernet`（已有）、httpx（已有）、React 19（无新依赖）、vitest。

## Global Constraints

以下值逐字来自 spec，每个任务的要求都隐含包含本节。

- 配置访问一律 `from app.config import settings` 单例。**本仓库没有 `get_settings()`**，全仓 grep 零命中。
- 会话有效期默认 **3 天**，**滑动刷新**：每个通过鉴权的请求重新签发 cookie。
- Cookie：`HttpOnly` 与 `SameSite=Strict` 固定；`Secure` 由 `PPTX2PDF_ADMIN_COOKIE_SECURE` 控制，**默认 `false`**（当前部署是 HTTP，写死 `true` 会导致登录后立刻掉线）。
- session 与 `client_secret` 加密**共用** `PPTX2PDF_SECRET_KEY`，不派生子密钥。代码注释须说明这是刻意判断，不是疏忽。
- `client_secret` **绝不回显**——不返回密文，也不返回解密值。修改时留空 = 沿用已存值；首次配置留空 → 422。
- **先测后存**：保存前跑完五步自检，全绿才写库，任何一步失败则库不动。**不提供跳过自检的旁路**。
- 自检**不复用 `GraphEngine`**。`GraphEngine` 刻意把错误归一为 `ConversionFailed`/`EngineUnavailable`/`ConversionTimeout` 以便流水线统一处理，而自检需要尽可能区分错误。只共用 `LOGIN_HOST` / `GRAPH_ROOT` 两个常量，URL 拼装自己写（`graph.py` 里那些是下划线私有函数，且没有自检需要的 `/sites/{id}/drive`）。
- 自检失败路径必须删除已上传的中转文件；**清理失败不得覆盖原始错误**（清理包在 `try/except` 内，异常只记日志）。
- python-pptx 只在 `requirements-dev.txt`，**不是生产依赖**。运行时读内置文件，不在运行时生成 pptx。
- 不引入前端路由库。不做账号体系、配额、风控。`Task.user_id` 继续留空。
- 测试判断标准：**这段逻辑能否在没有 Azure 账号的机器上运行？能则写测试。** 真实 HTTP 调用不写测试，留真机验证。
- 基线：后端 **227** passed，前端 **62** passed。任何既有测试不许变红。

## 既有代码的真实签名（已核对，勿凭记忆）

```python
# app/services/graph_credentials.py
@dataclass(frozen=True)
class GraphCredentialData:
    tenant_id: str
    client_id: str
    client_secret: str
    site_id: str
    drive_path: str

def load_credentials(session: Session) -> GraphCredentialData      # 未配置抛 GraphNotConfigured
def is_graph_configured(session: Session) -> bool
def save_credentials(
    session: Session, *, tenant_id: str, client_id: str,
    client_secret: str, site_id: str, drive_path: str = "pptx2pdf-staging",
) -> None                                                          # 关键字参数

# app/errors.py
class AppError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500
    def __init__(self, message: str = "") -> None: ...

# app/services/engines/graph.py（只用这两个常量）
LOGIN_HOST = "https://login.microsoftonline.com"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
```

`app/main.py` 已注册 `uploads.router` / `tasks.router` / `config.router`，startup 钩子在 64 行。`scripts/dump_openapi.py` 存在。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `backend/app/errors.py`（改） | 加 4 个错误类 |
| `backend/app/config.py`（改） | 加 3 个配置项 |
| `backend/app/services/admin_auth.py`（新） | 口令校验、cookie 签发/验证。不认识 Azure |
| `backend/app/services/graph_selftest.py`（新） | 五步自检。不认识 cookie，不写库 |
| `backend/app/assets/selftest.pptx`（新，二进制） | 内置单页测试文件 |
| `backend/app/api/admin.py`（新） | 四个端点，持有「先测后存」规则 |
| `backend/app/schemas.py`（改） | 管理端点的 DTO |
| `backend/app/main.py`（改） | 注册 admin router |
| `backend/scripts/make_selftest_pptx.py`（新） | 生成上述 pptx，开发时用 |
| `frontend/src/lib/adminApi.ts`（新） | 管理端点客户端 |
| `frontend/src/pages/AdminPage.tsx`（新） | 登录 + 表单 + 自检清单 |
| `frontend/src/App.tsx`（改） | 按 pathname 分发 |

---

## Task 1: 错误码与配置项

**Files:**
- Modify: `backend/app/errors.py`
- Modify: `backend/app/config.py`
- Modify: `.env.example`
- Test: `backend/tests/test_admin_config.py`

**Interfaces:**
- Produces: `AdminNotConfigured`、`AdminUnauthorized`、`AdminBadPassword`、`GraphSelftestFailed` 四个异常类；`settings.admin_password_hash`、`settings.admin_cookie_secure`、`settings.admin_session_days`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_admin_config.py`：

```python
from app.config import settings
from app.errors import (
    AdminBadPassword,
    AdminNotConfigured,
    AdminUnauthorized,
    GraphSelftestFailed,
)


def test_admin_error_codes_and_statuses():
    assert AdminNotConfigured.code == "ADMIN_NOT_CONFIGURED"
    assert AdminNotConfigured.http_status == 503
    assert AdminUnauthorized.code == "ADMIN_UNAUTHORIZED"
    assert AdminUnauthorized.http_status == 401
    assert AdminBadPassword.code == "ADMIN_BAD_PASSWORD"
    assert AdminBadPassword.http_status == 401
    assert GraphSelftestFailed.code == "GRAPH_SELFTEST_FAILED"
    assert GraphSelftestFailed.http_status == 422


def test_admin_settings_defaults():
    assert settings.admin_password_hash is None
    assert settings.admin_cookie_secure is False
    assert settings.admin_session_days == 3


def test_admin_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("PPTX2PDF_ADMIN_PASSWORD_HASH", "scrypt$aa$bb")
    monkeypatch.setenv("PPTX2PDF_ADMIN_COOKIE_SECURE", "true")
    monkeypatch.setenv("PPTX2PDF_ADMIN_SESSION_DAYS", "7")
    from app.config import Settings

    fresh = Settings()
    assert fresh.admin_password_hash == "scrypt$aa$bb"
    assert fresh.admin_cookie_secure is True
    assert fresh.admin_session_days == 7
```

- [ ] **Step 2: 跑测试确认失败**

工作目录 `backend/`：
```
.venv/Scripts/python.exe -m pytest tests/test_admin_config.py -v
```
预期：`ImportError: cannot import name 'AdminBadPassword' from 'app.errors'`

- [ ] **Step 3: 加错误类**

在 `backend/app/errors.py` 末尾追加：

```python
class AdminNotConfigured(AppError):
    """未设置 PPTX2PDF_ADMIN_PASSWORD_HASH。整个管理入口不可用——
    不提供「没设密码就免密进入」的默认行为，与 GraphNotConfigured 同构。"""

    code = "ADMIN_NOT_CONFIGURED"
    http_status = 503


class AdminUnauthorized(AppError):
    code = "ADMIN_UNAUTHORIZED"
    http_status = 401


class AdminBadPassword(AppError):
    code = "ADMIN_BAD_PASSWORD"
    http_status = 401


class GraphSelftestFailed(AppError):
    """五步连通性自检未全绿。响应体里带每步状态，调用方据此定位。"""

    code = "GRAPH_SELFTEST_FAILED"
    http_status = 422
```

- [ ] **Step 4: 加配置项**

在 `backend/app/config.py` 的 `Settings` 类里，紧挨 `secret_key` 之后加：

```python
    # 管理入口口令的 scrypt 哈希，格式 scrypt$<salt_hex>$<hash_hex>。
    # 未配置则管理入口整体 503——与 secret_key 一样不设默认值兜底。
    admin_password_hash: str | None = None
    # 当前部署是 http://<host>:18993，非 HTTPS。写死 Secure=true 会让浏览器
    # 根本不回传 cookie，表现为「登录成功但立刻掉线」且极难排查。
    # 切到 HTTPS 后必须改成 true。
    admin_cookie_secure: bool = False
    admin_session_days: int = 3
```

- [ ] **Step 5: 跑测试确认通过**

```
.venv/Scripts/python.exe -m pytest tests/test_admin_config.py -v
```
预期：3 passed

- [ ] **Step 6: 补 .env.example**

在 `.env.example` 的 Graph 引擎段落之后新增：

```
# ---- 管理入口（四期）----
# 管理页面 /admin 的口令哈希。未配置则管理入口整体返回 503。
# 生成（会提示你输入口令）：
#   python -c "import hashlib,os,binascii; s=os.urandom(16); pw=input('口令: ').encode(); print('scrypt$'+binascii.hexlify(s).decode()+'$'+binascii.hexlify(hashlib.scrypt(pw,salt=s,n=16384,r=8,p=1,dklen=32)).decode())"
PPTX2PDF_ADMIN_PASSWORD_HASH=
# 当前是 HTTP 部署，必须保持 false，否则浏览器不回传 cookie、登录后立刻掉线。
# 切到 HTTPS 后改成 true。
PPTX2PDF_ADMIN_COOKIE_SECURE=false
# 会话有效期（天）。每次通过鉴权的请求都会刷新，活跃使用不掉线。
PPTX2PDF_ADMIN_SESSION_DAYS=3
```

- [ ] **Step 7: 跑全量回归**

```
.venv/Scripts/python.exe -m pytest -q
```
预期：230 passed（227 基线 + 3 新增）

- [ ] **Step 8: 提交**

```bash
git add backend/app/errors.py backend/app/config.py backend/tests/test_admin_config.py .env.example
git commit -m "feat(backend): 管理入口的错误码与配置项"
```

---

## Task 2: admin_auth 服务

**Files:**
- Create: `backend/app/services/admin_auth.py`
- Test: `backend/tests/test_admin_auth.py`

**Interfaces:**
- Consumes: `settings.admin_password_hash`、`settings.admin_session_days`、`AdminNotConfigured`、`AdminBadPassword`、`AdminUnauthorized`
- Produces:
  ```python
  def hash_password(password: str, *, salt: bytes | None = None) -> str
  def verify_password(password: str) -> None      # 不抛即通过
  def issue_session() -> str                       # 返回 cookie 值
  def verify_session(token: str | None) -> None    # 不抛即通过
  SESSION_COOKIE_NAME = "pptx2pdf_admin"
  ```

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_admin_auth.py`：

```python
import time

import pytest

from app.errors import AdminBadPassword, AdminNotConfigured, AdminUnauthorized
from app.services import admin_auth


def _configure(monkeypatch, password: str = "hunter2") -> None:
    monkeypatch.setattr(
        admin_auth.settings, "admin_password_hash", admin_auth.hash_password(password)
    )


def test_hash_format():
    h = admin_auth.hash_password("hunter2")
    parts = h.split("$")
    assert parts[0] == "scrypt"
    assert len(parts) == 3
    assert len(bytes.fromhex(parts[1])) == 16
    assert len(bytes.fromhex(parts[2])) == 32


def test_hash_is_salted():
    assert admin_auth.hash_password("hunter2") != admin_auth.hash_password("hunter2")


def test_verify_password_accepts_correct(monkeypatch):
    _configure(monkeypatch)
    admin_auth.verify_password("hunter2")


def test_verify_password_rejects_wrong(monkeypatch):
    _configure(monkeypatch)
    with pytest.raises(AdminBadPassword):
        admin_auth.verify_password("wrong")


def test_verify_password_raises_when_not_configured(monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_password_hash", None)
    with pytest.raises(AdminNotConfigured):
        admin_auth.verify_password("hunter2")


def test_verify_password_raises_on_malformed_hash(monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_password_hash", "not-a-hash")
    with pytest.raises(AdminNotConfigured):
        admin_auth.verify_password("hunter2")


def test_session_roundtrip(monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_session_days", 3)
    token = admin_auth.issue_session()
    admin_auth.verify_session(token)


def test_verify_session_rejects_none():
    with pytest.raises(AdminUnauthorized):
        admin_auth.verify_session(None)


def test_verify_session_rejects_garbage():
    with pytest.raises(AdminUnauthorized):
        admin_auth.verify_session("not-a-token")


def test_verify_session_rejects_expired(monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_session_days", 3)
    token = admin_auth.issue_session()
    # Fernet 的 TTL 以 token 内嵌时间戳为准，把「现在」往后拨 4 天
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 4 * 86400)
    with pytest.raises(AdminUnauthorized):
        admin_auth.verify_session(token)
```

注意：`conftest.py` 已有为 `secret_key` 提供测试值的机制。若 `test_session_roundtrip` 因缺 `secret_key` 而报 `GraphNotConfigured`，在本文件加一个 autouse fixture 设置 `admin_auth.settings.secret_key` 为 `Fernet.generate_key().decode()`。

- [ ] **Step 2: 跑测试确认失败**

```
.venv/Scripts/python.exe -m pytest tests/test_admin_auth.py -v
```
预期：`ModuleNotFoundError: No module named 'app.services.admin_auth'`

- [ ] **Step 3: 实现**

创建 `backend/app/services/admin_auth.py`：

```python
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
    """返回 scrypt$<salt_hex>$<hash_hex>。salt 参数仅供测试构造确定值。"""
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
    return f"scrypt${binascii.hexlify(salt).decode()}${binascii.hexlify(digest).decode()}"


def verify_password(password: str) -> None:
    """口令正确则静默返回，否则抛异常。

    哈希缺失与哈希格式非法都归一成 AdminNotConfigured——两者对使用者
    是同一件事（管理入口没配好），而区分它们只会泄露配置细节。
    """
    stored = settings.admin_password_hash
    if not stored:
        raise AdminNotConfigured("未配置 PPTX2PDF_ADMIN_PASSWORD_HASH，管理入口不可用")

    parts = stored.split("$")
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


def issue_session() -> str:
    """签发会话 token。Fernet 自带时间戳，过期校验交给 decrypt 的 ttl。"""
    return _fernet().encrypt(b"admin").decode()


def verify_session(token: str | None) -> None:
    """token 有效则静默返回，否则抛 AdminUnauthorized。"""
    if not token:
        raise AdminUnauthorized("未登录")
    ttl = settings.admin_session_days * 86400
    try:
        _fernet().decrypt(token.encode(), ttl=ttl)
    except InvalidToken as exc:
        raise AdminUnauthorized("会话无效或已过期") from exc
```

- [ ] **Step 4: 跑测试确认通过**

```
.venv/Scripts/python.exe -m pytest tests/test_admin_auth.py -v
```
预期：10 passed

- [ ] **Step 5: 变异检查**

把 `verify_password` 里的 `hmac.compare_digest(actual, expected)` 改成 `True`，跑
```
.venv/Scripts/python.exe -m pytest tests/test_admin_auth.py -q
```
预期：`test_verify_password_rejects_wrong` 变红。确认后改回。

把 `verify_session` 的 `ttl=ttl` 去掉（改成 `decrypt(token.encode())`），预期 `test_verify_session_rejects_expired` 变红。确认后改回。

- [ ] **Step 6: 跑全量并提交**

```
.venv/Scripts/python.exe -m pytest -q
```
预期：240 passed

```bash
git add backend/app/services/admin_auth.py backend/tests/test_admin_auth.py
git commit -m "feat(backend): 管理入口的口令校验与会话签发"
```

---

## Task 3: 登录/登出端点与鉴权依赖

**Files:**
- Create: `backend/app/api/admin.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_admin_api.py`

**Interfaces:**
- Consumes: `admin_auth.verify_password/issue_session/verify_session/SESSION_COOKIE_NAME`
- Produces: `router`（前缀 `/api/admin`）、`require_admin` 依赖、`_set_session_cookie(response, token)`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_admin_api.py`：

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import admin_auth

PASSWORD = "hunter2"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(
        admin_auth.settings, "admin_password_hash", admin_auth.hash_password(PASSWORD)
    )
    monkeypatch.setattr(admin_auth.settings, "admin_cookie_secure", False)
    # 口令错误路径的 1 秒延迟在测试里没有意义，抹掉
    monkeypatch.setattr(admin_auth, "_WRONG_PASSWORD_DELAY_S", 0.0)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_login_sets_cookie(client):
    resp = client.post("/api/admin/login", json={"password": PASSWORD})
    assert resp.status_code == 204
    assert admin_auth.SESSION_COOKIE_NAME in resp.cookies


def test_login_rejects_wrong_password(client):
    resp = client.post("/api/admin/login", json={"password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "ADMIN_BAD_PASSWORD"


def test_login_503_when_password_not_configured(client, monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_password_hash", None)
    resp = client.post("/api/admin/login", json={"password": PASSWORD})
    assert resp.status_code == 503
    assert resp.json()["code"] == "ADMIN_NOT_CONFIGURED"


def test_cookie_flags(client):
    resp = client.post("/api/admin/login", json={"password": PASSWORD})
    raw = resp.headers["set-cookie"]
    assert "HttpOnly" in raw
    assert "SameSite=Strict" in raw
    assert "Secure" not in raw


def test_cookie_secure_follows_setting(client, monkeypatch):
    monkeypatch.setattr(admin_auth.settings, "admin_cookie_secure", True)
    resp = client.post("/api/admin/login", json={"password": PASSWORD})
    assert "Secure" in resp.headers["set-cookie"]


def test_logout_clears_cookie_without_auth(client):
    resp = client.post("/api/admin/logout")
    assert resp.status_code == 204
    # 未登录也能登出——语义是「清掉浏览器上的 cookie」，
    # cookie 已过期时同样应该成功
    assert 'pptx2pdf_admin=""' in resp.headers["set-cookie"] or "Max-Age=0" in resp.headers["set-cookie"]


def test_protected_endpoint_rejects_anonymous(client):
    resp = client.get("/api/admin/graph-credentials")
    assert resp.status_code == 401
    assert resp.json()["code"] == "ADMIN_UNAUTHORIZED"


def test_session_slides_on_each_request(client):
    client.post("/api/admin/login", json={"password": PASSWORD})
    first = client.cookies[admin_auth.SESSION_COOKIE_NAME]
    resp = client.get("/api/admin/graph-credentials")
    assert resp.status_code in (200, 404)
    assert "set-cookie" in resp.headers, "每个通过鉴权的请求都应重新签发 cookie"
    assert client.cookies[admin_auth.SESSION_COOKIE_NAME] != first
```

- [ ] **Step 2: 跑测试确认失败**

```
.venv/Scripts/python.exe -m pytest tests/test_admin_api.py -v
```
预期：全部 404（router 未注册）

- [ ] **Step 3: 实现端点**

创建 `backend/app/api/admin.py`：

```python
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
        samesite="strict",
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
        samesite="strict",
        secure=settings.admin_cookie_secure,
    )
    response.status_code = 204
    return response
```

在 `backend/app/schemas.py` 末尾加：

```python
class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)
```

在 `backend/app/main.py` 的 import 段加 `admin`，并在 `include_router` 那几行后加：

```python
app.include_router(admin.router)
```

- [ ] **Step 4: 跑测试确认通过**

```
.venv/Scripts/python.exe -m pytest tests/test_admin_api.py -v
```
预期：8 passed。`test_protected_endpoint_rejects_anonymous` 与 `test_session_slides_on_each_request` 依赖 Task 4 的 GET 端点，此时应因 404 而失败——**这是预期的**，把这两条标 `@pytest.mark.xfail(reason="GET 端点在 Task 4")`，Task 4 完成后去掉标记。

- [ ] **Step 5: 变异检查**

把 `require_admin` 里的 `_set_session_cookie(response, admin_auth.issue_session())` 删掉，跑 Task 4 完成后的测试，预期 `test_session_slides_on_each_request` 变红。**此检查在 Task 4 完成后补做**。

- [ ] **Step 6: 跑全量并提交**

```
.venv/Scripts/python.exe -m pytest -q
```

```bash
git add backend/app/api/admin.py backend/app/schemas.py backend/app/main.py backend/tests/test_admin_api.py
git commit -m "feat(backend): 管理入口的登录登出与鉴权依赖"
```

---

## Task 4: 读配置端点

**Files:**
- Modify: `backend/app/api/admin.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_admin_api.py`（追加）

**Interfaces:**
- Consumes: `require_admin`、`graph_credentials.load_credentials`、`GraphNotConfigured`
- Produces: `GET /api/admin/graph-credentials` → `GraphCredentialsDto`

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/test_admin_api.py` 追加。注意这里要真实建库，参考 `tests/test_graph_credentials.py` 现有的 session fixture 写法。

```python
from app.services import graph_credentials


def test_get_credentials_when_unset(client, admin_session):
    resp = client.get("/api/admin/graph-credentials")
    assert resp.status_code == 200
    body = resp.json()
    assert body["secret_configured"] is False
    assert body["tenant_id"] == ""
    assert body["drive_path"] == "pptx2pdf-staging"


def test_get_credentials_never_returns_secret(client, admin_session, db_session):
    graph_credentials.save_credentials(
        db_session,
        tenant_id="t-1",
        client_id="c-1",
        client_secret="SUPER-SECRET-VALUE",
        site_id="s-1",
        drive_path="staging",
    )
    resp = client.get("/api/admin/graph-credentials")
    body = resp.json()
    assert body["tenant_id"] == "t-1"
    assert body["client_id"] == "c-1"
    assert body["site_id"] == "s-1"
    assert body["drive_path"] == "staging"
    assert body["secret_configured"] is True
    # 密文与明文都不许出现在响应里
    assert "SUPER-SECRET-VALUE" not in resp.text
    assert "client_secret" not in body
    assert "client_secret_encrypted" not in body
```

`admin_session` fixture（加在文件里）：

```python
@pytest.fixture
def admin_session(client):
    client.post("/api/admin/login", json={"password": PASSWORD})
    return client
```

- [ ] **Step 2: 跑测试确认失败**

预期：404，端点不存在

- [ ] **Step 3: 实现**

在 `backend/app/schemas.py` 加：

```python
class GraphCredentialsDto(BaseModel):
    """读配置的响应。client_secret 绝不出现在这里——解密回显等于把凭证
    明文发到浏览器，那么加密存库本身就失去意义。"""

    tenant_id: str
    client_id: str
    site_id: str
    drive_path: str
    secret_configured: bool
```

在 `backend/app/api/admin.py` 加：

```python
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.errors import GraphNotConfigured
from app.schemas import GraphCredentialsDto
from app.services import graph_credentials


def _db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/graph-credentials", response_model=GraphCredentialsDto)
def get_graph_credentials(
    _: None = Depends(require_admin), db: Session = Depends(_db)
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
```

**必须在 `conftest.py` 补一条重定向。** 该文件已对 `db_module`、`pipeline_module`、`retention_module`、`shard_pipeline_module` 四处的 `SessionLocal` 做了 patch（第 43-56 行），理由写在注释里：模块顶层的 `from app.db import SessionLocal` 是早绑定，只 patch `db_module.SessionLocal` 改不到它。`app/api/admin.py` 的 `_db()` 同样在模块顶层导入，照第 56 行那条的写法加：

```python
    monkeypatch.setattr(admin_module, "SessionLocal", test_session_local)
```

不补这条，任何走到 `_db()` 的测试都会连上开发者本机的 `pptx2pdf.db`——一期踩过这个坑。

- [ ] **Step 4: 跑测试确认通过，并去掉 Task 3 的 xfail 标记**

```
.venv/Scripts/python.exe -m pytest tests/test_admin_api.py -v
```
预期：全部 passed

- [ ] **Step 5: 补做 Task 3 Step 5 的变异检查**

删掉 `require_admin` 里的重新签发那行，确认 `test_session_slides_on_each_request` 变红。改回。

- [ ] **Step 6: 跑全量并提交**

```bash
git add backend/app/api/admin.py backend/app/schemas.py backend/tests/test_admin_api.py backend/tests/conftest.py
git commit -m "feat(backend): 读取 Graph 凭证配置，secret 不回显"
```

---

## Task 5: 自检的诊断映射

**Files:**
- Create: `backend/app/services/graph_selftest.py`
- Test: `backend/tests/test_graph_selftest_diagnose.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class StepResult:
      step: str
      ok: bool | None
      detail: str | None
  STEPS = ("token", "drive", "upload", "convert", "delete")
  def diagnose_token_error(status: int, body: str) -> str
  def diagnose_graph_error(step: str, status: int, body: str) -> str
  ```

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_graph_selftest_diagnose.py`：

```python
from app.services.graph_selftest import (
    STEPS,
    diagnose_graph_error,
    diagnose_token_error,
)


def test_steps_order():
    assert STEPS == ("token", "drive", "upload", "convert", "delete")


def test_token_error_unknown_tenant():
    msg = diagnose_token_error(400, '{"error":"invalid_request","error_description":"AADSTS90002: Tenant not found"}')
    assert "租户" in msg
    assert "AADSTS90002" in msg


def test_token_error_bad_client_id():
    msg = diagnose_token_error(400, '{"error_description":"AADSTS700016: Application not found in directory"}')
    assert "client_id" in msg
    assert "AADSTS700016" in msg


def test_token_error_bad_secret():
    msg = diagnose_token_error(401, '{"error_description":"AADSTS7000215: Invalid client secret provided"}')
    assert "client_secret" in msg
    assert "AADSTS7000215" in msg


def test_token_error_falls_back_to_raw():
    msg = diagnose_token_error(500, "internal server error")
    assert "500" in msg
    assert "internal server error" in msg


def test_drive_404_points_at_site_id():
    msg = diagnose_graph_error("drive", 404, '{"error":{"code":"itemNotFound"}}')
    assert "site_id" in msg


def test_drive_403_points_at_permission():
    msg = diagnose_graph_error("drive", 403, '{"error":{"code":"accessDenied"}}')
    assert "权限" in msg


def test_delete_403_names_the_known_permission_trap():
    msg = diagnose_graph_error("delete", 403, '{"error":{"code":"accessDenied"}}')
    assert "Files.ReadWrite.All" in msg or "Sites.ReadWrite.All" in msg


def test_diagnose_truncates_long_body():
    msg = diagnose_graph_error("upload", 500, "x" * 5000)
    assert len(msg) < 500
```

- [ ] **Step 2: 跑测试确认失败**

预期：`ModuleNotFoundError`

- [ ] **Step 3: 实现（只实现本任务的纯函数部分）**

创建 `backend/app/services/graph_selftest.py`：

```python
"""Graph 凭证的五步连通性自检。

不复用 GraphEngine：那边刻意把错误归一成 ConversionFailed /
EngineUnavailable / ConversionTimeout 以便流水线统一处理，而这里
需要的恰好相反——尽可能区分错误，还要知道卡在第几步、Graph 返回的
原始 error code 是什么。复用只有两条路：改造 GraphEngine 让它吐更多
细节（污染一个已为转换优化好的接口），或在这里解析归一后的中文消息
字符串（脆弱）。代价是一部分 HTTP 调用逻辑重复，判断依据是诊断价值
高于这部分重复的维护成本。
"""

from dataclasses import dataclass

STEPS = ("token", "drive", "upload", "convert", "delete")

_BODY_LIMIT = 300


@dataclass
class StepResult:
    step: str
    ok: bool | None
    """None 表示因前序步骤失败而未执行，与 False（执行了但失败）区分。"""
    detail: str | None


def _clip(body: str) -> str:
    return body if len(body) <= _BODY_LIMIT else body[:_BODY_LIMIT] + "…"


def diagnose_token_error(status: int, body: str) -> str:
    """AADSTS 错误码能区分租户、client_id、client_secret 三类错误。"""
    if "AADSTS90002" in body:
        return f"租户不存在或 tenant_id 写错（AADSTS90002）。原始响应：{_clip(body)}"
    if "AADSTS700016" in body:
        return f"该租户下找不到这个应用，client_id 写错或应用未创建（AADSTS700016）。原始响应：{_clip(body)}"
    if "AADSTS7000215" in body:
        return f"client_secret 错误或已过期（AADSTS7000215）。原始响应：{_clip(body)}"
    return f"取 access token 失败（HTTP {status}）。原始响应：{_clip(body)}"


def diagnose_graph_error(step: str, status: int, body: str) -> str:
    if step == "drive":
        if status == 404:
            return f"找不到该站点或站点下的文档库，site_id 可能写错（HTTP 404）。原始响应：{_clip(body)}"
        if status == 403:
            return f"应用对该站点没有权限，检查 API 权限是否已授予且管理员同意已点（HTTP 403）。原始响应：{_clip(body)}"
    if step == "upload" and status == 403:
        return f"没有写权限，无法上传到中转库（HTTP 403）。原始响应：{_clip(body)}"
    if step == "delete" and status == 403:
        return (
            "没有永久删除权限（HTTP 403）。permanentDelete 需要 "
            "Files.ReadWrite.All 或 Sites.ReadWrite.All，Sites.Selected 不够。"
            f"原始响应：{_clip(body)}"
        )
    return f"{step} 步失败（HTTP {status}）。原始响应：{_clip(body)}"
```

- [ ] **Step 4: 跑测试确认通过**

```
.venv/Scripts/python.exe -m pytest tests/test_graph_selftest_diagnose.py -v
```
预期：9 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/graph_selftest.py backend/tests/test_graph_selftest_diagnose.py
git commit -m "feat(backend): 自检的分步诊断映射"
```

---

## Task 6: 内置测试文件与五步自检编排

**Files:**
- Create: `backend/scripts/make_selftest_pptx.py`
- Create: `backend/app/assets/selftest.pptx`（脚本生成后提交）
- Modify: `backend/app/services/graph_selftest.py`
- Test: `backend/tests/test_graph_selftest_flow.py`

**Interfaces:**
- Consumes: `GraphCredentialData`、`LOGIN_HOST`、`GRAPH_ROOT`、`StepResult`、诊断函数
- Produces: `def run_selftest(creds: GraphCredentialData, *, client_factory=httpx.Client) -> list[StepResult]`

- [ ] **Step 1: 写生成脚本并生成 pptx**

创建 `backend/scripts/make_selftest_pptx.py`：

```python
"""生成自检用的内置 pptx。开发时跑一次，产物提交进仓库。

运行时不需要 python-pptx（它只在 requirements-dev.txt），所以这个
文件是资产而不是运行时生成的。

用法（工作目录 backend/）：
    .venv/Scripts/python.exe scripts/make_selftest_pptx.py
"""

from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt

MARKER = "pptx2pdf selftest"
DEST = Path(__file__).resolve().parent.parent / "app" / "assets" / "selftest.pptx"


def main() -> None:
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 空白版式
    box = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(11), Inches(1.5))
    frame = box.text_frame
    frame.text = MARKER
    frame.paragraphs[0].runs[0].font.size = Pt(40)

    DEST.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(DEST))
    print(f"wrote {DEST} ({DEST.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
```

运行：
```
.venv/Scripts/python.exe scripts/make_selftest_pptx.py
```
预期输出：`wrote .../app/assets/selftest.pptx (约 30000 bytes)`

- [ ] **Step 2: 写失败的测试**

创建 `backend/tests/test_graph_selftest_flow.py`。用一个假 client 驱动编排，不碰网络：

```python
from dataclasses import dataclass

import httpx
import pytest

from app.services.graph_credentials import GraphCredentialData
from app.services.graph_selftest import SELFTEST_PPTX, run_selftest

CREDS = GraphCredentialData(
    tenant_id="t-1",
    client_id="c-1",
    client_secret="s-1",
    site_id="site-1",
    drive_path="staging",
)


@dataclass
class _Call:
    method: str
    url: str


class _FakeClient:
    """按 (method, url 关键字) 匹配预设响应。未匹配的调用直接失败，
    这样编排形状一变就会被发现，而不是静默走进兜底。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[_Call] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _respond(self, method, url, **kwargs):
        self.calls.append(_Call(method, url))
        for key, resp in self.routes:
            if key in url:
                return resp() if callable(resp) else resp
        raise AssertionError(f"_FakeClient: 没配置的 {method} {url}")

    def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._respond("PUT", url, **kwargs)

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._respond(method, url, **kwargs)


def _resp(status, *, json_body=None, content=b""):
    return httpx.Response(
        status_code=status,
        json=json_body,
        content=None if json_body is not None else content,
        request=httpx.Request("GET", "https://example.invalid"),
    )


def _pdf_bytes() -> bytes:
    return SELFTEST_PPTX.read_bytes()[:8] + b"%PDF-1.4 fake"


def _all_green_routes():
    return [
        ("oauth2/v2.0/token", _resp(200, json_body={"access_token": "tok", "expires_in": 3600})),
        ("/drive", _resp(200, json_body={"id": "drive-1"})),
        ("createUploadSession", _resp(200, json_body={"uploadUrl": "https://upload.example/x"})),
        ("upload.example", _resp(201, json_body={"id": "item-1"})),
        ("format=pdf", _resp(200, content=b"%PDF-1.4 hello")),
        ("permanentDelete", _resp(204)),
    ]


def test_all_green():
    fake = _FakeClient(_all_green_routes())
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert [r.step for r in results] == ["token", "drive", "upload", "convert", "delete"]
    assert all(r.ok is True for r in results)


def test_token_failure_short_circuits():
    fake = _FakeClient([("oauth2/v2.0/token", _resp(401, json_body={"error_description": "AADSTS7000215: bad secret"}))])
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[0].ok is False
    assert "client_secret" in results[0].detail
    # 后四步未执行，用 None 而不是 False
    assert [r.ok for r in results[1:]] == [None, None, None, None]


def test_drive_failure_short_circuits():
    routes = _all_green_routes()
    routes[1] = ("/drive", _resp(404, json_body={"error": {"code": "itemNotFound"}}))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[0].ok is True
    assert results[1].ok is False
    assert "site_id" in results[1].detail
    assert [r.ok for r in results[2:]] == [None, None, None]


def test_convert_failure_still_deletes():
    routes = _all_green_routes()
    routes[4] = ("format=pdf", _resp(500, content=b"boom"))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[3].ok is False
    # 转换失败也必须清理已上传的文件，否则每次失败的自检都在租户里留垃圾
    assert any("permanentDelete" in c.url for c in fake.calls)


def test_cleanup_failure_does_not_mask_convert_failure():
    routes = _all_green_routes()
    routes[4] = ("format=pdf", _resp(500, content=b"boom"))
    routes[5] = ("permanentDelete", _resp(403, json_body={"error": {"code": "accessDenied"}}))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[3].ok is False
    assert "500" in results[3].detail
    assert results[4].ok is False


def test_convert_rejects_non_pdf():
    routes = _all_green_routes()
    routes[4] = ("format=pdf", _resp(200, content=b"<html>Sign in</html>"))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[3].ok is False
    assert "PDF" in results[3].detail


def test_staging_filename_has_recognizable_prefix():
    fake = _FakeClient(_all_green_routes())
    run_selftest(CREDS, client_factory=lambda **kw: fake)
    session_call = next(c for c in fake.calls if "createUploadSession" in c.url)
    assert "pptx2pdf-selftest-" in session_call.url


def test_selftest_pptx_asset_exists():
    assert SELFTEST_PPTX.is_file()
    assert SELFTEST_PPTX.stat().st_size > 0
```

- [ ] **Step 3: 跑测试确认失败**

预期：`ImportError: cannot import name 'run_selftest'`

- [ ] **Step 4: 实现编排**

在 `backend/app/services/graph_selftest.py` 追加：

```python
import logging
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.engines.graph import GRAPH_ROOT, LOGIN_HOST
from app.services.graph_credentials import GraphCredentialData

logger = logging.getLogger(__name__)

SELFTEST_PPTX = Path(__file__).resolve().parent.parent / "assets" / "selftest.pptx"

# 固定前缀：进程若在上传与删除之间被杀，能人工识别并清理租户里的残留。
_STAGING_PREFIX = "pptx2pdf-selftest-"


def _drive_url(site_id: str) -> str:
    return f"{GRAPH_ROOT}/sites/{quote(site_id, safe='')}/drive"


def _upload_session_url(site_id: str, drive_path: str, filename: str) -> str:
    site = quote(site_id, safe="")
    target = f"{drive_path}/{filename}"
    return f"{GRAPH_ROOT}/sites/{site}/drive/root:/{quote(target, safe='/')}:/createUploadSession"


def _content_url(site_id: str, item_id: str) -> str:
    site = quote(site_id, safe="")
    return f"{GRAPH_ROOT}/sites/{site}/drive/items/{quote(item_id, safe='')}/content?format=pdf"


def _delete_url(site_id: str, item_id: str) -> str:
    site = quote(site_id, safe="")
    return f"{GRAPH_ROOT}/sites/{site}/drive/items/{quote(item_id, safe='')}/permanentDelete"


def run_selftest(
    creds: GraphCredentialData, *, client_factory=httpx.Client
) -> list[StepResult]:
    """跑完五步，返回每步结果。任何一步失败则后续步骤 ok=None。

    永远返回列表、不抛异常——调用方要的是诊断清单而不是一个异常。
    """
    results = {step: StepResult(step=step, ok=None, detail=None) for step in STEPS}
    timeout = settings.graph_request_timeout_s

    with client_factory(timeout=timeout, follow_redirects=True) as client:
        token = _step_token(client, creds, results)
        if token is None:
            return [results[s] for s in STEPS]

        headers = {"Authorization": f"Bearer {token}"}
        if not _step_drive(client, creds, headers, results):
            return [results[s] for s in STEPS]

        item_id = _step_upload(client, creds, headers, results)
        if item_id is None:
            return [results[s] for s in STEPS]

        _step_convert(client, creds, headers, item_id, results)
        _step_delete(client, creds, headers, item_id, results)

    return [results[s] for s in STEPS]


def _step_token(client, creds, results) -> str | None:
    url = f"{LOGIN_HOST}/{quote(creds.tenant_id, safe='')}/oauth2/v2.0/token"
    try:
        resp = client.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
    except httpx.HTTPError as exc:
        results["token"] = StepResult("token", False, f"网络错误：{type(exc).__name__}")
        return None
    if resp.status_code != 200:
        results["token"] = StepResult("token", False, diagnose_token_error(resp.status_code, resp.text))
        return None
    token = resp.json().get("access_token")
    if not token:
        results["token"] = StepResult("token", False, "响应里没有 access_token")
        return None
    results["token"] = StepResult("token", True, None)
    return token


def _step_drive(client, creds, headers, results) -> bool:
    try:
        resp = client.get(_drive_url(creds.site_id), headers=headers)
    except httpx.HTTPError as exc:
        results["drive"] = StepResult("drive", False, f"网络错误：{type(exc).__name__}")
        return False
    if resp.status_code != 200:
        results["drive"] = StepResult("drive", False, diagnose_graph_error("drive", resp.status_code, resp.text))
        return False
    results["drive"] = StepResult("drive", True, None)
    return True


def _step_upload(client, creds, headers, results) -> str | None:
    filename = f"{_STAGING_PREFIX}{uuid.uuid4().hex}.pptx"
    payload = SELFTEST_PPTX.read_bytes()
    try:
        session_resp = client.post(
            _upload_session_url(creds.site_id, creds.drive_path, filename),
            headers=headers,
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        if session_resp.status_code not in (200, 201):
            results["upload"] = StepResult(
                "upload", False, diagnose_graph_error("upload", session_resp.status_code, session_resp.text)
            )
            return None
        upload_url = session_resp.json()["uploadUrl"]

        total = len(payload)
        put_resp = client.put(
            upload_url,
            headers={
                "Content-Length": str(total),
                "Content-Range": f"bytes 0-{total - 1}/{total}",
            },
            content=payload,
        )
    except httpx.HTTPError as exc:
        results["upload"] = StepResult("upload", False, f"网络错误：{type(exc).__name__}")
        return None
    if put_resp.status_code not in (200, 201):
        results["upload"] = StepResult(
            "upload", False, diagnose_graph_error("upload", put_resp.status_code, put_resp.text)
        )
        return None
    item_id = put_resp.json().get("id")
    if not item_id:
        results["upload"] = StepResult("upload", False, "上传完成但响应里没有 driveItem id")
        return None
    results["upload"] = StepResult("upload", True, None)
    return item_id


def _step_convert(client, creds, headers, item_id, results) -> None:
    try:
        resp = client.get(_content_url(creds.site_id, item_id), headers=headers)
    except httpx.HTTPError as exc:
        results["convert"] = StepResult("convert", False, f"网络错误：{type(exc).__name__}")
        return
    if resp.status_code != 200:
        results["convert"] = StepResult(
            "convert", False, diagnose_graph_error("convert", resp.status_code, resp.text)
        )
        return
    if not resp.content.startswith(b"%PDF"):
        results["convert"] = StepResult(
            "convert", False, "转换返回的内容不是 PDF——可能被重定向到了登录页"
        )
        return
    results["convert"] = StepResult("convert", True, None)


def _step_delete(client, creds, headers, item_id, results) -> None:
    """无论转换成功与否都要跑——失败路径上的中转文件同样必须删掉。"""
    try:
        resp = client.post(_delete_url(creds.site_id, item_id), headers=headers)
    except httpx.HTTPError as exc:
        results["delete"] = StepResult("delete", False, f"网络错误：{type(exc).__name__}")
        return
    if resp.status_code not in (200, 204):
        results["delete"] = StepResult(
            "delete", False, diagnose_graph_error("delete", resp.status_code, resp.text)
        )
        return
    results["delete"] = StepResult("delete", True, None)
```

- [ ] **Step 5: 跑测试确认通过**

```
.venv/Scripts/python.exe -m pytest tests/test_graph_selftest_flow.py -v
```
预期：8 passed

- [ ] **Step 6: 变异检查**

逐个做，每次改回：

1. `_step_convert` 里删掉 `if not resp.content.startswith(b"%PDF")` 整段 → 预期 `test_convert_rejects_non_pdf` 变红
2. `run_selftest` 里把 `_step_delete(...)` 那行删掉 → 预期 `test_convert_failure_still_deletes` 变红
3. `_step_upload` 的 `filename` 改成 `f"{uuid.uuid4().hex}.pptx"`（去掉前缀）→ 预期 `test_staging_filename_has_recognizable_prefix` 变红
4. `_step_token` 失败时改成 `return ""` 而不是 `None` → 预期 `test_token_failure_short_circuits` 变红

- [ ] **Step 7: 跑全量并提交**

```bash
git add backend/scripts/make_selftest_pptx.py backend/app/assets/selftest.pptx backend/app/services/graph_selftest.py backend/tests/test_graph_selftest_flow.py
git commit -m "feat(backend): 五步连通性自检与内置测试文件"
```

---

## Task 7: 存配置端点（先测后存）

**Files:**
- Modify: `backend/app/api/admin.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_admin_api.py`（追加）

**Interfaces:**
- Consumes: `run_selftest`、`save_credentials`、`load_credentials`、`GraphSelftestFailed`
- Produces: `PUT /api/admin/graph-credentials`

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/test_admin_api.py` 追加：

```python
from app.services import graph_selftest
from app.services.graph_selftest import StepResult

_GREEN = [StepResult(s, True, None) for s in graph_selftest.STEPS]


def _stub_selftest(monkeypatch, results):
    calls = []

    def fake(creds, **kwargs):
        calls.append(creds)
        return results

    monkeypatch.setattr(graph_selftest, "run_selftest", fake)
    monkeypatch.setattr("app.api.admin.run_selftest", fake)
    return calls


def test_put_runs_selftest_before_saving(client, admin_session, db_session, monkeypatch):
    calls = _stub_selftest(monkeypatch, _GREEN)
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "t-1", "client_id": "c-1", "client_secret": "s-1",
            "site_id": "site-1", "drive_path": "staging",
        },
    )
    assert resp.status_code == 200
    assert len(calls) == 1, "保存前必须跑自检"
    saved = graph_credentials.load_credentials(db_session)
    assert saved.tenant_id == "t-1"
    assert saved.client_secret == "s-1"


def test_put_does_not_save_when_selftest_fails(client, admin_session, db_session, monkeypatch):
    failing = [
        StepResult("token", True, None),
        StepResult("drive", False, "site_id 写错"),
        StepResult("upload", None, None),
        StepResult("convert", None, None),
        StepResult("delete", None, None),
    ]
    _stub_selftest(monkeypatch, failing)
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "t-1", "client_id": "c-1", "client_secret": "s-1",
            "site_id": "bad", "drive_path": "staging",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "GRAPH_SELFTEST_FAILED"
    assert body["steps"][1]["ok"] is False
    assert body["steps"][2]["ok"] is None
    # 库里一个字节都不许动
    with pytest.raises(GraphNotConfigured):
        graph_credentials.load_credentials(db_session)


def test_put_blank_secret_reuses_stored(client, admin_session, db_session, monkeypatch):
    graph_credentials.save_credentials(
        db_session, tenant_id="old-t", client_id="old-c",
        client_secret="STORED-SECRET", site_id="old-s", drive_path="old-d",
    )
    calls = _stub_selftest(monkeypatch, _GREEN)
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "new-t", "client_id": "new-c", "client_secret": "",
            "site_id": "new-s", "drive_path": "new-d",
        },
    )
    assert resp.status_code == 200
    # 自检拿到的必须是库里的旧 secret
    assert calls[0].client_secret == "STORED-SECRET"
    saved = graph_credentials.load_credentials(db_session)
    assert saved.client_secret == "STORED-SECRET"
    assert saved.tenant_id == "new-t"


def test_put_blank_secret_rejected_on_first_config(client, admin_session, monkeypatch):
    _stub_selftest(monkeypatch, _GREEN)
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "t", "client_id": "c", "client_secret": "",
            "site_id": "s", "drive_path": "d",
        },
    )
    assert resp.status_code == 422
    assert "client_secret" in resp.text


def test_put_rejects_anonymous(client):
    resp = client.put(
        "/api/admin/graph-credentials",
        json={
            "tenant_id": "t", "client_id": "c", "client_secret": "s",
            "site_id": "s", "drive_path": "d",
        },
    )
    assert resp.status_code == 401
```

- [ ] **Step 2: 跑测试确认失败**

预期：405 或 404（PUT 端点不存在）

- [ ] **Step 3: 实现**

在 `backend/app/schemas.py` 加：

```python
class GraphCredentialsUpdate(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    client_id: str = Field(min_length=1, max_length=64)
    client_secret: str = Field(default="", max_length=512)
    """留空表示沿用库中已存的值。首次配置时留空会被拒绝。"""
    site_id: str = Field(min_length=1, max_length=256)
    drive_path: str = Field(default="pptx2pdf-staging", min_length=1, max_length=256)


class SelftestStepDto(BaseModel):
    step: str
    ok: bool | None
    detail: str | None


class SelftestResultDto(BaseModel):
    ok: bool
    steps: list[SelftestStepDto]
```

在 `backend/app/api/admin.py` 加：

```python
from app.errors import GraphSelftestFailed, ValidationError
from app.schemas import GraphCredentialsUpdate, SelftestResultDto, SelftestStepDto
from app.services.graph_credentials import GraphCredentialData
from app.services.graph_selftest import run_selftest


@router.put("/graph-credentials", response_model=SelftestResultDto)
def put_graph_credentials(
    payload: GraphCredentialsUpdate,
    _: None = Depends(require_admin),
    db: Session = Depends(_db),
) -> SelftestResultDto:
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

    # 先测后存：配错的凭证一旦入库，三期的引擎每次转换都会去读它，
    # 期间所有走 Graph 的任务都会失败，而失败原因与「你配错了」之间
    # 隔着数层抽象。这条顺序把那个窗口彻底关掉。
    results = run_selftest(creds)
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
```

`GraphSelftestFailed` 需要带上 steps。修改 `backend/app/errors.py` 里该类：

```python
class GraphSelftestFailed(AppError):
    """五步连通性自检未全绿。响应体里带每步状态，调用方据此定位。"""

    code = "GRAPH_SELFTEST_FAILED"
    http_status = 422

    def __init__(self, message: str = "", *, steps: list | None = None) -> None:
        super().__init__(message or "Graph 凭证自检未通过")
        self.steps = steps or []
```

并在 `main.py` 的 `AppError` 处理器里，若异常有 `steps` 属性则一并放进响应体。查看现有处理器的写法后照改。

- [ ] **Step 4: 跑测试确认通过**

```
.venv/Scripts/python.exe -m pytest tests/test_admin_api.py -v
```

- [ ] **Step 5: 变异检查（本任务的验收核心）**

1. 把 `results = run_selftest(creds)` 改成 `results = [StepResult(s, True, None) for s in STEPS]`（跳过自检）→ 预期 `test_put_runs_selftest_before_saving` 变红
2. 把 `if not all(r.ok for r in results): raise ...` 整段删掉 → 预期 `test_put_does_not_save_when_selftest_fails` 变红
3. 把「留空沿用旧值」那段改成 `secret = payload.client_secret`（留空即清空）→ 预期 `test_put_blank_secret_reuses_stored` 变红

- [ ] **Step 6: 跑全量并提交**

```bash
git add backend/app/api/admin.py backend/app/schemas.py backend/app/errors.py backend/app/main.py backend/tests/test_admin_api.py
git commit -m "feat(backend): 存配置端点，自检全绿才写库"
```

---

## Task 8: 前端管理页面与路由分发

**Files:**
- Create: `frontend/src/lib/adminApi.ts`
- Create: `frontend/src/pages/AdminPage.tsx`
- Create: `frontend/src/pages/AdminPage.test.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles/tokens.css`（若需新令牌）

**Interfaces:**
- Consumes: `/api/admin/*` 四个端点
- Produces: `AdminPage` 组件；`App.tsx` 按 `pathname` 分发

- [ ] **Step 1: 写 adminApi 客户端**

创建 `frontend/src/lib/adminApi.ts`：

```typescript
export interface GraphCredentials {
  tenant_id: string
  client_id: string
  site_id: string
  drive_path: string
  secret_configured: boolean
}

export interface SelftestStep {
  step: string
  ok: boolean | null
  detail: string | null
}

export interface SelftestResult {
  ok: boolean
  steps: SelftestStep[]
}

export const STEP_LABELS: Record<string, string> = {
  token: '取 access token',
  drive: '访问中转库',
  upload: '上传测试文件',
  convert: '转换为 PDF',
  delete: '永久删除测试文件',
}

async function parseError(resp: Response): Promise<never> {
  const body = await resp.json().catch(() => ({}))
  const err = new Error(body.message || `HTTP ${resp.status}`) as Error & {
    code?: string
    steps?: SelftestStep[]
  }
  err.code = body.code
  err.steps = body.steps
  throw err
}

export async function login(password: string): Promise<void> {
  const resp = await fetch('/api/admin/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
  if (!resp.ok) await parseError(resp)
}

export async function logout(): Promise<void> {
  await fetch('/api/admin/logout', { method: 'POST' })
}

export async function getCredentials(): Promise<GraphCredentials> {
  const resp = await fetch('/api/admin/graph-credentials')
  if (!resp.ok) await parseError(resp)
  return resp.json()
}

export async function putCredentials(payload: {
  tenant_id: string
  client_id: string
  client_secret: string
  site_id: string
  drive_path: string
}): Promise<SelftestResult> {
  const resp = await fetch('/api/admin/graph-credentials', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!resp.ok) await parseError(resp)
  return resp.json()
}
```

- [ ] **Step 2: 写失败的测试**

创建 `frontend/src/pages/AdminPage.test.tsx`：

```tsx
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AdminPage } from './AdminPage'

const CREDS = {
  tenant_id: 't-1',
  client_id: 'c-1',
  site_id: 's-1',
  drive_path: 'staging',
  secret_configured: true,
}

function mockFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    handler(String(input), init),
  )
  vi.stubGlobal('fetch', spy)
  return spy
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AdminPage', () => {
  it('未登录时显示口令输入框', async () => {
    mockFetch(() => json({ code: 'ADMIN_UNAUTHORIZED' }, 401))
    render(<AdminPage />)
    expect(await screen.findByLabelText('管理口令')).toBeTruthy()
  })

  it('已登录时加载并显示配置，secret 显示为已配置而不回显', async () => {
    mockFetch((url) => (url.includes('graph-credentials') ? json(CREDS) : json({}, 204)))
    render(<AdminPage />)
    await waitFor(() => expect((screen.getByLabelText('租户 ID') as HTMLInputElement).value).toBe('t-1'))
    expect(screen.getByText(/已配置（不回显）/)).toBeTruthy()
  })

  it('口令错误时显示错误而不进入表单', async () => {
    mockFetch((url) => {
      if (url.includes('/login')) return json({ code: 'ADMIN_BAD_PASSWORD', message: '口令错误' }, 401)
      return json({ code: 'ADMIN_UNAUTHORIZED' }, 401)
    })
    render(<AdminPage />)
    const input = await screen.findByLabelText('管理口令')
    await userEvent.type(input, 'wrong')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))
    expect(await screen.findByText(/口令错误/)).toBeTruthy()
  })

  it('保存成功后展示五步全绿', async () => {
    const green = {
      ok: true,
      steps: [
        { step: 'token', ok: true, detail: null },
        { step: 'drive', ok: true, detail: null },
        { step: 'upload', ok: true, detail: null },
        { step: 'convert', ok: true, detail: null },
        { step: 'delete', ok: true, detail: null },
      ],
    }
    mockFetch((url, init) => {
      if (init?.method === 'PUT') return json(green)
      if (url.includes('graph-credentials')) return json(CREDS)
      return json({}, 204)
    })
    render(<AdminPage />)
    await waitFor(() => screen.getByLabelText('租户 ID'))
    await userEvent.click(screen.getByRole('button', { name: '测试并保存' }))
    await waitFor(() => expect(screen.getAllByText('通过').length).toBe(5))
  })

  it('自检失败时逐步展示诊断，未执行的步骤显示为未执行', async () => {
    const failing = {
      code: 'GRAPH_SELFTEST_FAILED',
      message: 'Graph 凭证自检未通过',
      steps: [
        { step: 'token', ok: true, detail: null },
        { step: 'drive', ok: false, detail: '找不到该站点，site_id 可能写错' },
        { step: 'upload', ok: null, detail: null },
        { step: 'convert', ok: null, detail: null },
        { step: 'delete', ok: null, detail: null },
      ],
    }
    mockFetch((url, init) => {
      if (init?.method === 'PUT') return json(failing, 422)
      if (url.includes('graph-credentials')) return json(CREDS)
      return json({}, 204)
    })
    render(<AdminPage />)
    await waitFor(() => screen.getByLabelText('租户 ID'))
    await userEvent.click(screen.getByRole('button', { name: '测试并保存' }))
    expect(await screen.findByText(/site_id 可能写错/)).toBeTruthy()
    expect(screen.getAllByText('未执行').length).toBe(3)
  })

  it('自检进行中禁用保存按钮', async () => {
    let release: (v: Response) => void = () => {}
    const pending = new Promise<Response>((r) => (release = r))
    mockFetch((url, init) => {
      if (init?.method === 'PUT') return pending
      if (url.includes('graph-credentials')) return json(CREDS)
      return json({}, 204)
    })
    render(<AdminPage />)
    await waitFor(() => screen.getByLabelText('租户 ID'))
    const btn = screen.getByRole('button', { name: '测试并保存' })
    await userEvent.click(btn)
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(true))
    release(json({ ok: true, steps: [] }))
  })
})
```

- [ ] **Step 3: 跑测试确认失败**

工作目录 `frontend/`：
```
npm test -- --run src/pages/AdminPage.test.tsx
```
预期：`Failed to resolve import "./AdminPage"`

- [ ] **Step 4: 实现 AdminPage**

创建 `frontend/src/pages/AdminPage.tsx`。要点：

- 两个状态：`authed === false` 渲染口令表单，`true` 渲染配置表单
- 挂载时先 `getCredentials()` 探测登录态：成功 → 已登录并填充表单；`code === 'ADMIN_UNAUTHORIZED'` → 未登录；`code === 'ADMIN_NOT_CONFIGURED'` → 显示「管理入口未配置口令」提示
- `client_secret` 输入框 `type="password"`，`secret_configured` 为真时 placeholder 显示「已配置（不回显），留空则沿用」
- 五步清单用 `STEP_LABELS` 渲染，状态映射：`true` → 「通过」，`false` → 「失败」+ detail，`null` → 「未执行」
- 保存中 `disabled={saving}` 且按钮文案变「自检中…」——五步真实网络调用可能耗时若干秒，无反馈会被误认为卡死
- 样式复用现有 `--c-*` 设计令牌，不新增视觉风格。已核对 `tokens.css`：通过用 `--c-success`（浅色 `#12805c`）配 `--c-success-soft`，失败用 `--c-danger` 配 `--c-danger-soft`，未执行用中性文字色。三者浅深两套主题都有定义，不要新增令牌

- [ ] **Step 5: 跑测试确认通过**

```
npm test -- --run src/pages/AdminPage.test.tsx
```
预期：6 passed

- [ ] **Step 6: App.tsx 按 pathname 分发**

在 `frontend/src/App.tsx` 顶部加：

```tsx
import { AdminPage } from './pages/AdminPage'
```

在 `App` 组件最开头加：

```tsx
  // 只有两个页面，一个 pathname 判断足够——不引入 react-router 这个依赖
  if (typeof window !== 'undefined' && window.location.pathname.startsWith('/admin')) {
    return <AdminPage />
  }
```

- [ ] **Step 7: 补 App 分发的测试**

在 `frontend/src/App.test.tsx` 追加：

```tsx
it('/admin 路径渲染管理页而不是上传界面', async () => {
  const original = window.location
  Object.defineProperty(window, 'location', {
    value: { ...original, pathname: '/admin' },
    writable: true,
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      new Response(JSON.stringify({ code: 'ADMIN_UNAUTHORIZED' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  )
  render(<App />)
  expect(await screen.findByLabelText('管理口令')).toBeTruthy()
  Object.defineProperty(window, 'location', { value: original, writable: true })
})
```

- [ ] **Step 8: 跑前端全量与构建**

```
npm test -- --run
npm run build
```
预期：69 passed（62 基线 + 6 + 1），build 成功

- [ ] **Step 9: 变异检查**

1. `AdminPage` 里把 `secret_configured` 那个 placeholder 分支删掉 → 预期「secret 显示为已配置」那条变红
2. `App.tsx` 的 pathname 判断改成恒 `false` → 预期 `/admin 路径渲染管理页` 变红
3. 保存按钮的 `disabled={saving}` 删掉 → 预期「自检进行中禁用保存按钮」变红

- [ ] **Step 10: 提交**

```bash
git add frontend/src/lib/adminApi.ts frontend/src/pages/AdminPage.tsx frontend/src/pages/AdminPage.test.tsx frontend/src/App.tsx frontend/src/App.test.tsx
git commit -m "feat(frontend): 管理页面与 /admin 路由分发"
```

---

## Task 9: openapi 漂移守卫、Nginx fallback 与文档

**Files:**
- Create: `backend/tests/test_openapi_snapshot.py`
- Modify: `backend/openapi.json`（重新生成）
- Modify: `deploy/nginx.conf`（确认 `/admin` 落 SPA fallback）
- Modify: `README.md`
- Modify: `docker-compose.yml`（若需要传新环境变量）

**Interfaces:**
- Consumes: 前八个任务的全部产出

- [ ] **Step 1: 写 openapi 漂移守卫测试**

创建 `backend/tests/test_openapi_snapshot.py`：

```python
"""守住 openapi.json 与实际路由的一致性。

三期 Task 8 曾漏过一次快照重新生成，靠人工执行才发现——仓库当时
对此没有任何自动守卫。
"""

import json
from pathlib import Path

from app.main import app

SNAPSHOT = Path(__file__).resolve().parent.parent / "openapi.json"


def test_openapi_snapshot_is_current():
    current = app.openapi()
    stored = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert stored == current, (
        "openapi.json 与当前路由不一致。"
        "在 backend/ 下跑 .venv/Scripts/python.exe -m scripts.dump_openapi 重新生成。"
    )
```

- [ ] **Step 2: 跑测试确认失败**

```
.venv/Scripts/python.exe -m pytest tests/test_openapi_snapshot.py -v
```
预期：FAIL——四期新增了四个端点，快照还是旧的

- [ ] **Step 3: 重新生成快照**

```
.venv/Scripts/python.exe -m scripts.dump_openapi
```

- [ ] **Step 4: 跑测试确认通过**

```
.venv/Scripts/python.exe -m pytest tests/test_openapi_snapshot.py -v
```
预期：1 passed

- [ ] **Step 5: 确认 Nginx 的 SPA fallback 覆盖 /admin**

**已核对，无需改动。** 配置文件是 `deploy/nginx-frontend.conf`（不是 `nginx.conf`），第 11-13 行已有：

```
    location / {
        try_files $uri $uri/ /index.html;
```

`/admin` 会落到 SPA fallback，直接访问不会 404。本步只需跑一遍确认没被后续改动破坏，不要动这个文件。

- [ ] **Step 6: 补 README**

在 README 新增「管理入口」一节，内容必须包含：

1. 设口令的完整命令（与 `.env.example` 里那条一致）
2. 访问 `http://<host>:<port>/admin` 登录
3. 五步自检各自的含义与常见失败原因（照 Task 5 的诊断文本写）
4. `permanentDelete` 权限那个坑：`Sites.Selected` 不够，需 `Files.ReadWrite.All` 或 `Sites.ReadWrite.All`
5. **`PPTX2PDF_ADMIN_COOKIE_SECURE` 与 HTTPS 的关系**：当前 HTTP 部署必须保持 `false`，切 HTTPS 后改 `true`
6. **改完 `.env` 必须 `docker compose up -d` 重启**——`settings` 是模块级单例，只在 import 时读一次环境

在「关键配置」表补三个新变量。

把「已知限制」里「Graph 通道在四期管理页面上线前不可达」那条改写为：Graph 通道需先在 `/admin` 完成配置并通过自检；未配置时引擎返回 `GRAPH_NOT_CONFIGURED`。

在「已有部署升级」那条提示里，把三个新变量加进需要 `diff .env .env.example` 的说明。

- [ ] **Step 7: 检查 compose 是否需要改**

`api` 与 `worker` 都有 `env_file: .env`，会把 `.env` 全部内容注入容器，**三个新变量不需要在 `environment:` 块里显式声明**（三期已验证过这个链路）。跑一遍确认没写坏：

```
docker compose config -q
```
预期：exit 0

- [ ] **Step 8: 跑前后端全量**

```
cd backend && .venv/Scripts/python.exe -m pytest -q
cd ../frontend && npm test -- --run && npm run build
```

- [ ] **Step 9: 提交**

```bash
git add backend/tests/test_openapi_snapshot.py backend/openapi.json README.md deploy/nginx.conf
git commit -m "test(backend): openapi 漂移守卫；docs: 管理入口文档"
```

---

## 自审记录

**Spec 覆盖检查：**

| spec 章节 | 对应任务 |
|---|---|
| §4 鉴权（口令、会话、cookie 属性、暴力破解） | Task 1、2、3 |
| §5 凭证配置数据流（不回显、留空沿用、先测后存） | Task 4、7 |
| §6 五步自检（诊断、清理、不复用引擎、内置 pptx） | Task 5、6 |
| §8 API 契约（四个端点、错误码、结果结构） | Task 3、4、7 |
| §9 前端（pathname 分发、两态、清单、进行中禁用） | Task 8 |
| §10 三个配置项 | Task 1 |
| §11 测试策略与三条接线守护 | Task 2 Step 5、Task 6 Step 6、Task 7 Step 5、Task 8 Step 9 |
| §11 openapi 漂移守卫 | Task 9 |
| §13 部署与文档 | Task 9 |

三条接线守护的落点：先测后存 → Task 7 Step 5-1/2；cookie 滑动刷新 → Task 4 Step 5；留空沿用旧 secret → Task 7 Step 5-3。全部有对应的变异步骤。

**类型一致性：** `StepResult(step, ok, detail)` 在 Task 5 定义，Task 6、7 使用；`GraphCredentialData` 字段顺序与既有定义一致；`save_credentials` 的关键字参数调用与真实签名一致。

**写计划时逐一核对过的事实**（三期的教训：那份计划文档引用了全仓不存在的 `splitting` 状态、不存在的 `purge_expired_shards`、8 处过时测试计数，靠三重过滤才没污染代码，代价是三次往返。本计划的每个引用都当场 grep 过）：

- `save_credentials` 是关键字参数签名、`drive_path` 有默认值 —— 已核对
- `AppError` 基类只有 `code` / `http_status` / `__init__(message="")` —— 已核对，四个新错误类照此继承
- 已有错误码里没有 `ADMIN_*` 与 `GRAPH_SELFTEST_FAILED`，`ValidationError` 已存在可复用 —— 已核对
- `graph.py` 的 URL 拼装全是下划线私有函数，且没有 `/sites/{id}/drive` —— 已核对，故自检自己写拼装，只共用 `LOGIN_HOST` / `GRAPH_ROOT` 两个常量
- `conftest.py` 已 patch 四处 `SessionLocal`，第 56 行是最近的先例 —— 已核对，Task 4 照此加第五处
- `tokens.css` 有 `--c-success` / `--c-danger` / `--c-warn` 及各自的 `-soft`，浅深两套主题齐全 —— 已核对
- `deploy/nginx-frontend.conf` 第 11-13 行已有 SPA fallback —— 已核对，`/admin` 不需要改 nginx
- `scripts/dump_openapi.py` 存在 —— 已核对
- 基线 227 / 62 —— 实测

**唯一的计划内跨任务依赖**：Task 3 的 `test_protected_endpoint_rejects_anonymous` 与 `test_session_slides_on_each_request` 需要 Task 4 的 GET 端点才能跑。Task 3 Step 4 已写明先标 `xfail`，Task 4 Step 4 写明去掉标记、Step 5 补做 Task 3 遗留的变异检查。
