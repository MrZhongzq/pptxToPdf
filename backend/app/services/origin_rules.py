"""来源规则的语法解析与匹配。

白名单与黑名单共用这一套语法：

    example.com                              精确
    *.example.com                            通配：任意子域（不含裸域本身）
    203.0.113.7                              IP
    *.example.com||@except{a.example.com}    通配但排除某些子域
    *.example.com||@match{api.example.com}   通配范围内只允许列出的
    example.com||@no_graph                   该来源不得使用 graph 引擎

修饰符用 `||@` 引导，可以叠加：`*.a.com||@match{x.a.com}||@no_graph`。
`@except` / `@match` 的多个值用逗号分隔。

## 为什么不支持完整正则

需求原文提到「通配符和正则语法」，但给出的四个例子全是通配与集合运算，
没有一个真正的正则。放开正则会引入两个真问题：

1. **ReDoS**——一条写得不好的规则（`(a+)+$` 这类）就能让每次请求都卡住，
   而规则是从数据库读的，等于把服务可用性交给了规则的作者。
2. 管理员写错一个字符就可能把范围从「一个子域」放大到「全网」，而白名单
   的错误方向恰恰是静默放行。

`*` 通配 + `@except` + `@match` 覆盖了需求给出的全部用例。真需要正则时
应该显式加一个 `re:` 前缀并配超时保护，而不是让所有规则都走正则引擎。
"""

import re
from dataclasses import dataclass, field

_MODIFIER_RE = re.compile(r"\|\|@(\w+)(?:\{([^}]*)\})?")


@dataclass(frozen=True)
class OriginRule:
    """一条解析好的规则。"""

    pattern: str
    """host 部分，可能含 `*`。已小写。"""
    excepts: frozenset[str] = frozenset()
    matches: frozenset[str] = frozenset()
    no_graph: bool = False
    raw: str = ""

    def matches_host(self, host: str) -> bool:
        """host 是否落在这条规则的范围内。

        顺序：先看 pattern 是否命中，再用 @match 收窄，最后用 @except 减去。
        @match 与 @except 同时出现时先算 match——它把范围缩到只剩列出的那些，
        随后 except 才有东西可减。
        """
        host = (host or "").strip().lower()
        if not host:
            return False
        if not _host_matches_pattern(host, self.pattern):
            return False
        if self.matches and host not in self.matches:
            return False
        if host in self.excepts:
            return False
        return True


@dataclass
class RuleSet:
    """一组规则，按「任一命中即命中」求并。"""

    rules: list[OriginRule] = field(default_factory=list)

    def find(self, host: str) -> OriginRule | None:
        """返回第一条命中的规则，没有则 None。

        返回规则本身而不是布尔值：调用方还需要读它的修饰符（比如
        `@no_graph`），只回答「在不在里面」不够用。
        """
        for rule in self.rules:
            if rule.matches_host(host):
                return rule
        return None

    def matches(self, host: str) -> bool:
        return self.find(host) is not None

    def __bool__(self) -> bool:
        return bool(self.rules)


def _host_matches_pattern(host: str, pattern: str) -> bool:
    if pattern == host:
        return True
    if not pattern.startswith("*."):
        return False
    # `*.example.com` 匹配任意子域，但**不含** example.com 本身。
    # 这是刻意的：想同时覆盖裸域就再写一条 example.com。让通配隐式包含
    # 裸域会让「只想放开子域」变得无法表达。
    suffix = pattern[1:]  # ".example.com"
    if not host.endswith(suffix):
        return False
    # 前缀非空即可——`*.example.com` 匹配任意层级子域（a.example.com 与
    # a.b.example.com 都算）。要精确到某一层就直接写全，那正是用户举的
    # `abc.zz.xx.yy.com` 那种形式。
    return bool(host[: -len(suffix)])


def parse_rule(raw: str) -> OriginRule:
    """解析一条规则。语法非法时抛 ValueError，由调用方转成用户可见的错误。"""
    text = (raw or "").strip()
    if not text:
        raise ValueError("规则不能为空")

    modifiers = _MODIFIER_RE.findall(text)
    pattern = _MODIFIER_RE.sub("", text).strip().lower()

    if not pattern:
        raise ValueError("规则缺少域名或 IP 部分")
    if "/" in pattern or "://" in pattern:
        raise ValueError("只填域名或 IP（可带端口），不要带协议或路径")
    if "*" in pattern and not pattern.startswith("*."):
        raise ValueError("通配只支持 `*.` 开头的形式，例如 *.example.com")

    excepts: set[str] = set()
    matches: set[str] = set()
    no_graph = False
    for name, arg in modifiers:
        if name == "except":
            excepts |= _split_list(arg)
        elif name == "match":
            matches |= _split_list(arg)
        elif name == "no_graph":
            no_graph = True
        else:
            raise ValueError(f"未知修饰符 @{name}（支持 @except{{}} / @match{{}} / @no_graph）")

    return OriginRule(
        pattern=pattern,
        excepts=frozenset(excepts),
        matches=frozenset(matches),
        no_graph=no_graph,
        raw=text,
    )


def _split_list(arg: str) -> set[str]:
    return {p.strip().lower() for p in (arg or "").split(",") if p.strip()}


def build(raws: list[str]) -> RuleSet:
    """批量解析。单条坏掉不拖垮整组——它只是不生效。

    黑白名单是从数据库读的，一条历史遗留的坏规则不该让整个访问控制失效
    （对白名单是全部拒绝，对黑名单是全部放行，两个方向都很糟）。
    """
    rules = []
    for raw in raws:
        try:
            rules.append(parse_rule(raw))
        except ValueError:
            continue
    return RuleSet(rules)


def normalize_host(value: str | None) -> str | None:
    """从 Origin/Referer/Host 里取出 host[:port]，小写。"""
    if not value:
        return None
    text = value.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    return text.lower() or None


def strip_port(host: str | None) -> str | None:
    """去掉端口。IPv6 的 [::1]:8080 也要处理。"""
    if not host:
        return None
    if host.startswith("["):
        return host.split("]", 1)[0][1:]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host
