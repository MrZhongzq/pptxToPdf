"""防跨站：拒绝来源不在白名单里的写请求。

六期先建骨架，默认**不启用**——这个功能眼下没有实际用途，配错却能把
自己关在门外（当前部署是 IP 直连，没有域名）。所以有两道保险：
`origin_guard_enabled` 默认 False，且白名单为空时即使开启也放行。
"""

import logging
from urllib.parse import urlparse

from app.config import settings
from app.models import AllowedOrigin

logger = logging.getLogger(__name__)

# 只校验会改变状态的请求。跨站攻击的实质是诱导浏览器发起带凭证的**写**
# 请求；拦 GET 只会破坏图片、下载等正常用法，收益为零。
GUARDED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def extract_host(header_value: str | None) -> str | None:
    """从 Origin 或 Referer 取出 host[:port]，小写。

    比对只看 host[:port] 不看协议：同一台机器的 http 与 https 是同一个
    来源，让管理员为此配两条只会平添困惑。
    """
    if not header_value:
        return None
    parsed = urlparse(header_value)
    if parsed.netloc:
        return parsed.netloc.lower()
    # Origin 理论上总是带协议，但 Referer 可能是裸 host，宽容处理
    return header_value.split("/")[0].strip().lower() or None


def is_allowed(host: str | None, allowed: set[str]) -> bool:
    """host 是否在白名单里。

    缺失 Origin 与 Referer 时放行：同源的表单提交、curl、以及部分浏览器
    的隐私模式都不带这两个头。把「没有头」当成跨站会误伤大量正常请求，
    而真正的跨站攻击场景里浏览器一定会带上 Origin。
    """
    if host is None:
        return True
    return host in allowed


def load_allowed(session) -> set[str]:
    return {row.origin for row in session.query(AllowedOrigin).all()}


def should_check(method: str) -> bool:
    return method.upper() in GUARDED_METHODS
