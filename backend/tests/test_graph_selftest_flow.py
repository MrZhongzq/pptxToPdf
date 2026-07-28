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
        matches = [(key, resp) for key, resp in self.routes if key in url]
        if not matches:
            raise AssertionError(f"_FakeClient: 没配置的 {method} {url}")
        # Graph 的端点是层级嵌套的（/drive 是下游所有 URL 的前缀），
        # 所以要匹配最具体的那条路由而不是列表里最靠前的那条。
        key, resp = max(matches, key=lambda kv: len(kv[0]))
        return resp() if callable(resp) else resp

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
