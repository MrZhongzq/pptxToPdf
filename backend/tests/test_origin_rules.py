"""来源规则的语法与匹配。

这套语法同时驱动白名单（谁能用 v1）和黑名单（谁一律 403），一条匹配
错误的后果是「本该拦的放进来了」或「本该进来的被拦在外面」，所以每条
语义都要有测试守着。
"""

import pytest

from app.services.origin_rules import (
    build,
    normalize_host,
    parse_rule,
    strip_port,
)


# ---- 精确与通配 ----


def test_exact_match():
    r = parse_rule("example.com")
    assert r.matches_host("example.com") is True
    assert r.matches_host("a.example.com") is False


def test_wildcard_matches_subdomains():
    r = parse_rule("*.example.com")
    assert r.matches_host("a.example.com") is True
    assert r.matches_host("api.v2.example.com") is True


def test_wildcard_excludes_the_bare_domain():
    """`*.example.com` 不含 example.com 本身。

    刻意如此：让通配隐式包含裸域，「只想放开子域」就无法表达了。
    要同时覆盖就再写一条 example.com。
    """
    assert parse_rule("*.example.com").matches_host("example.com") is False


def test_wildcard_does_not_match_a_different_suffix():
    """notexample.com 不能被 *.example.com 命中——后缀比对必须带上那个点，
    否则一条规则会意外放开一批看起来相似的域名。"""
    r = parse_rule("*.example.com")
    assert r.matches_host("notexample.com") is False
    assert r.matches_host("evil-example.com") is False


def test_deep_subdomain_exact_rule():
    """用户举的 abc.zz.xx.yy.com 这种精确子域形式。"""
    r = parse_rule("abc.zz.xx.yy.com")
    assert r.matches_host("abc.zz.xx.yy.com") is True
    assert r.matches_host("other.zz.xx.yy.com") is False


def test_ip_rule():
    r = parse_rule("203.0.113.7")
    assert r.matches_host("203.0.113.7") is True
    assert r.matches_host("203.0.113.8") is False


def test_case_insensitive():
    assert parse_rule("EXAMPLE.com").matches_host("example.COM") is True


# ---- @except ----


def test_except_subtracts_from_the_wildcard():
    r = parse_rule("*.example.com||@except{a.example.com}")
    assert r.matches_host("b.example.com") is True
    assert r.matches_host("a.example.com") is False


def test_except_accepts_multiple_values():
    r = parse_rule("*.example.com||@except{a.example.com,b.example.com}")
    assert r.matches_host("a.example.com") is False
    assert r.matches_host("b.example.com") is False
    assert r.matches_host("c.example.com") is True


# ---- @match ----


def test_match_narrows_to_only_listed():
    """@match 是白名单式收窄：范围里只剩列出的那些。"""
    r = parse_rule("*.example.com||@match{api.example.com}")
    assert r.matches_host("api.example.com") is True
    assert r.matches_host("www.example.com") is False


def test_match_and_except_compose():
    """两者同时出现时先算 match（把范围缩小），再算 except（从中减去）。"""
    r = parse_rule("*.example.com||@match{a.example.com,b.example.com}||@except{b.example.com}")
    assert r.matches_host("a.example.com") is True
    assert r.matches_host("b.example.com") is False
    assert r.matches_host("c.example.com") is False


# ---- @no_graph ----


def test_no_graph_is_a_modifier_not_a_matcher():
    """@no_graph 不影响「是否放行」，只影响「放行之后能不能用 graph」。"""
    r = parse_rule("example.com||@no_graph")
    assert r.matches_host("example.com") is True
    assert r.no_graph is True


def test_no_graph_defaults_off():
    assert parse_rule("example.com").no_graph is False


def test_modifiers_stack():
    r = parse_rule("*.a.com||@match{x.a.com}||@no_graph")
    assert r.matches_host("x.a.com") is True
    assert r.no_graph is True


# ---- 语法错误 ----


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "http://example.com",
        "example.com/path",
        "ex*ample.com",
        "*example.com",
        "example.com||@nope",
    ],
)
def test_rejects_bad_syntax(bad):
    with pytest.raises(ValueError):
        parse_rule(bad)


def test_error_message_names_the_unknown_modifier():
    """错误消息要说清支持哪些修饰符——管理员写错时看得到正确写法。"""
    with pytest.raises(ValueError, match="@except"):
        parse_rule("example.com||@bogus")


# ---- RuleSet ----


def test_ruleset_is_a_union():
    rs = build(["a.com", "*.b.com"])
    assert rs.matches("a.com") is True
    assert rs.matches("x.b.com") is True
    assert rs.matches("c.com") is False


def test_ruleset_returns_the_matched_rule_so_modifiers_are_readable():
    """只回答「在不在里面」不够用——调用方还要读 @no_graph。"""
    rs = build(["a.com||@no_graph", "b.com"])
    assert rs.find("a.com").no_graph is True
    assert rs.find("b.com").no_graph is False
    assert rs.find("c.com") is None


def test_one_broken_rule_does_not_kill_the_set():
    """规则从数据库读，一条历史遗留的坏规则不该让整个访问控制失效——
    对白名单是全部拒绝，对黑名单是全部放行，两个方向都很糟。"""
    rs = build(["a.com", "http://broken", "b.com"])
    assert rs.matches("a.com") is True
    assert rs.matches("b.com") is True
    assert len(rs.rules) == 2


def test_empty_ruleset_is_falsy():
    assert not build([])
    assert build(["a.com"])


# ---- host 归一 ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://example.com", "example.com"),
        ("https://example.com:8443/x/y", "example.com:8443"),
        ("EXAMPLE.com", "example.com"),
        ("example.com", "example.com"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_host(raw, expected):
    assert normalize_host(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("example.com:8443", "example.com"),
        ("example.com", "example.com"),
        ("[::1]:8080", "::1"),
        ("203.0.113.7:80", "203.0.113.7"),
    ],
)
def test_strip_port(raw, expected):
    assert strip_port(raw) == expected
