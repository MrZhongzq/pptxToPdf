"""v1 的下载器与 SSRF 防护。

这是整条 v1 接口最大的风险面：fileUrl 由调用方给，服务器会去请求它。
不设防的话任何人都能让这台机器访问它自己的内网——包括云厂商的元数据
端点（169.254.169.254 能拿到实例凭证）。每条防护都要有测试守着，尤其是
「每一跳重定向都重新校验」那条：只查首个 URL 等于没查。
"""

import socket

import pytest

from app.services.url_fetch import UrlNotAllowed, assert_public_url


def _mock_transport(monkeypatch, uf, handler):
    """把 httpx 的传输层换成 MockTransport，不碰真实网络。

    保留 follow_redirects=False：url_fetch 手工跟随重定向正是为了在每一跳
    上重新跑 assert_public_url，交给 httpx 自动跟就绕过了那道校验。
    """
    import httpx

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(**kwargs):
        kwargs.pop("follow_redirects", None)
        kwargs.pop("transport", None)
        return real_client(transport=transport, follow_redirects=False, **kwargs)

    monkeypatch.setattr(uf.httpx, "Client", fake_client)


def _resolve_to(monkeypatch, mapping: dict[str, list[str]]):
    """把 DNS 解析替换成固定映射，避免测试依赖真实网络。"""
    import app.services.url_fetch as uf

    # patch getaddrinfo 而不是 _resolved_addresses：后者是包着 try 的那一层，
    # 替换掉它就等于把「解析失败 -> UrlNotAllowed」那段转换逻辑一起换掉了，
    # 测不到真实路径。
    def fake_getaddrinfo(hostname, *_args, **_kwargs):
        if hostname not in mapping:
            raise socket.gaierror("no such host")
        return [(None, None, None, None, (addr, 0)) for addr in mapping[hostname]]

    monkeypatch.setattr(uf.socket, "getaddrinfo", fake_getaddrinfo)


# ---- 协议 ----


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "gopher://x"])
def test_rejects_non_http_schemes(url, monkeypatch):
    _resolve_to(monkeypatch, {"x": ["8.8.8.8"]})
    with pytest.raises(UrlNotAllowed, match="http"):
        assert_public_url(url)


def test_rejects_url_without_host(monkeypatch):
    with pytest.raises(UrlNotAllowed):
        assert_public_url("http:///just/a/path")


# ---- 内网地址 ----


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",       # 环回
        "10.1.2.3",        # 私有 A
        "172.16.5.4",      # 私有 B
        "192.168.1.1",     # 私有 C
        "169.254.169.254", # 云元数据——不拦这个等于把实例凭证送出去
        "0.0.0.0",         # 未指定
        "::1",             # IPv6 环回
        "fe80::1",         # IPv6 链路本地
        "fc00::1",         # IPv6 唯一本地
    ],
)
def test_rejects_private_and_special_addresses(addr, monkeypatch):
    _resolve_to(monkeypatch, {"evil.example": [addr]})
    with pytest.raises(UrlNotAllowed, match="内网或保留"):
        assert_public_url("http://evil.example/a.pptx")


def test_allows_public_address(monkeypatch):
    _resolve_to(monkeypatch, {"good.example": ["8.8.8.8"]})
    assert_public_url("https://good.example/a.pptx")  # 不抛即通过


def test_rejects_when_any_resolved_address_is_private(monkeypatch):
    """一个域名可以解析出多个 A 记录。只要有一个指向内网就拒绝——
    否则攻击者只要让第二条记录指向内网，再赌一次连接选中它。"""
    _resolve_to(monkeypatch, {"mixed.example": ["8.8.8.8", "127.0.0.1"]})
    with pytest.raises(UrlNotAllowed):
        assert_public_url("http://mixed.example/a.pptx")


def test_rejects_unresolvable_host(monkeypatch):
    _resolve_to(monkeypatch, {})
    with pytest.raises(UrlNotAllowed):
        assert_public_url("http://nowhere.example/a.pptx")


def test_error_message_does_not_leak_the_internal_ip(monkeypatch):
    """不回显解析出的具体 IP：那等于把内网拓扑当成一个探测接口送出去。"""
    _resolve_to(monkeypatch, {"evil.example": ["10.9.8.7"]})
    with pytest.raises(UrlNotAllowed) as exc:
        assert_public_url("http://evil.example/a.pptx")
    assert "10.9.8.7" not in str(exc.value)


# ---- 重定向：每一跳都要重新校验 ----


def test_redirect_to_private_address_is_rejected(monkeypatch, tmp_path):
    """「返回 302 指向 169.254.169.254」是最常见的 SSRF 绕过手法。
    只在第一跳校验的实现会在这里放行。"""
    import httpx

    import app.services.url_fetch as uf

    _resolve_to(
        monkeypatch,
        {"public.example": ["8.8.8.8"], "meta.example": ["169.254.169.254"]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://meta.example/creds"})
        return httpx.Response(200, content=b"secrets")

    _mock_transport(monkeypatch, uf, handler)

    with pytest.raises(UrlNotAllowed):
        uf.fetch_to("http://public.example/a.pptx", tmp_path / "out.pptx")


def test_follows_a_safe_redirect(monkeypatch, tmp_path):
    import httpx

    import app.services.url_fetch as uf

    _resolve_to(
        monkeypatch,
        {"a.example": ["8.8.8.8"], "b.example": ["1.1.1.1"]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example":
            return httpx.Response(302, headers={"location": "http://b.example/real.pptx"})
        return httpx.Response(200, content=b"PPTX-BYTES")

    _mock_transport(monkeypatch, uf, handler)

    result = uf.fetch_to("http://a.example/a.pptx", tmp_path / "out.pptx")

    assert result.size_bytes == len(b"PPTX-BYTES")
    assert result.filename == "real.pptx"


# ---- 体积上限 ----


def test_rejects_when_body_exceeds_limit_even_without_content_length(monkeypatch, tmp_path):
    """Content-Length 是源站说的，可以撒谎也可以干脆不给。必须边下边判。"""
    import httpx

    import app.services.url_fetch as uf
    from app.services.url_fetch import DownloadTooLarge

    monkeypatch.setattr(uf.settings, "max_file_size", 1024)
    _resolve_to(monkeypatch, {"big.example": ["8.8.8.8"]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    _mock_transport(monkeypatch, uf, handler)

    with pytest.raises(DownloadTooLarge):
        uf.fetch_to("http://big.example/a.pptx", tmp_path / "out.pptx")

    assert not (tmp_path / "out.pptx").exists(), "超限时不该留下半份文件"
