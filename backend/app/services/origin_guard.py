"""访问控制：黑名单（全站）与来源白名单（只管 v1）。

## 两者的分工

- **黑名单**：网页与 v1 一起拦，命中直接 403。**优先级最高**——先查它，
  命中即返回，不再进入任何后续判断。
- **白名单**：只约束 `/v1/*` 这条给脚本用的接口。**网页永不受影响**。

七期把白名单的作用域从「所有写请求」收窄到「只管 v1」，这推翻了六期的
一条设计。六期做成全站校验时，为了防止管理员配错把自己关在门外，加了
「白名单为空时放行」这道保险；现在网页根本不走白名单，那个风险不存在了，
于是这道保险也一并去掉——**空白名单现在的语义是「v1 谁也不许用」**，
这是一个清晰且安全的默认。
"""

import ipaddress
import logging

from sqlalchemy.orm import Session

from app.models import AllowedOrigin, BlockedOrigin
from app.services import origin_rules
from app.services.origin_rules import RuleSet

logger = logging.getLogger(__name__)

V1_PREFIX = "/v1/"


def load_allowed(session: Session) -> RuleSet:
    return origin_rules.build([r.origin for r in session.query(AllowedOrigin).all()])


def load_blocked(session: Session) -> RuleSet:
    return origin_rules.build([r.origin for r in session.query(BlockedOrigin).all()])


def is_v1(path: str) -> bool:
    return path.startswith(V1_PREFIX)


def candidate_hosts(client_ip: str | None, origin: str | None, referer: str | None) -> list[str]:
    """一个请求可以用来判定来源的所有依据，按可信度排序。

    客户端 IP 排在最前：v1 的调用方多半是脚本而不是浏览器，根本不会带
    Origin；只看头部等于没有防护。Origin/Referer 补充域名维度——同一个
    IP 上可能跑着多个域名。
    """
    hosts: list[str] = []
    if client_ip:
        hosts.append(client_ip.lower())
    for header in (origin, referer):
        host = origin_rules.normalize_host(header)
        if host:
            hosts.append(host)
            bare = origin_rules.strip_port(host)
            if bare and bare != host:
                hosts.append(bare)
    return hosts


def match_any(rules: RuleSet, hosts: list[str]):
    """任一 host 命中即返回那条规则。"""
    for host in hosts:
        rule = rules.find(host)
        if rule is not None:
            return rule
    return None


def is_private_address(host: str) -> bool:
    """host 是不是内网/环回/链路本地地址。

    给 v1 的 SSRF 防护用（见 services/url_fetch），这里只做纯判断。
    非 IP 字面量返回 False——域名要先解析成 IP 再问这个函数。
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )
