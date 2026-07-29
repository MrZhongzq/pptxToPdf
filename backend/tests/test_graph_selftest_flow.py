from dataclasses import dataclass

import httpx

from app.services import graph_selftest
from app.services.graph_credentials import GraphCredentialData
from app.services.graph_selftest import STEPS, SELFTEST_PPTX, run_selftest

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
    这样编排形状一变就会被发现，而不是静默走进兜底。

    _respond 用「最长 key 优先」代替「列表里第一个匹配」，因为 Graph 的
    端点是层级嵌套的（/drive 是下游 upload/convert/delete URL 的公共
    前缀）。这是长度启发式，不是真正的特异性判定——当前 6 条路由表里
    最长恰好等于最具体，但这只是巧合。新增路由时如果引入了一条比现有
    某条更长、但实际匹配范围更宽的 key（例如给 site_id 相关的路径加一条
    通用前缀），长度顺序可能不再等于特异性顺序，需要自查。"""

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


def _raise(exc: Exception):
    """让 _FakeClient 的某条路由在被调用时抛异常，而不是返回响应——
    用来驱动 httpx.HTTPError 网络异常分支（超时/连接失败/DNS 失败等），
    这类分支之前完全没有测试触达过。"""

    def _inner():
        raise exc

    return _inner


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


# ---- C1: run_selftest 必须永远返回列表，即使异常不是 httpx.HTTPError ----
# 五个场景对应审查列出的五处：client_factory 抛错 / pptx 文件不存在 /
# token 响应非 JSON / createUploadSession 缺 uploadUrl 键 / 上传 PUT 响应非 JSON。
# 每个都必须证明 run_selftest 没有把异常穿透给调用方。


def test_client_factory_failure_returns_list_not_raises():
    def _raising_factory(**kw):
        raise RuntimeError("cannot build client: bad proxy config")

    results = run_selftest(CREDS, client_factory=_raising_factory)
    assert [r.step for r in results] == list(STEPS)
    assert results[0].ok is None  # 连接都没建起来，不是「第一步执行了但失败」
    assert "RuntimeError" in results[0].detail
    assert all(r.ok is None for r in results)


def test_selftest_pptx_missing_returns_list_not_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(graph_selftest, "SELFTEST_PPTX", tmp_path / "missing.pptx")
    fake = _FakeClient(_all_green_routes())
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[2].ok is False
    assert "读取内置自检文件失败" in results[2].detail
    assert [r.ok for r in results[3:]] == [None, None]


def test_token_non_json_response_returns_list_not_raises():
    fake = _FakeClient([("oauth2/v2.0/token", _resp(200, content=b"<html>login</html>"))])
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[0].ok is False
    assert "JSON" in results[0].detail
    assert [r.ok for r in results[1:]] == [None, None, None, None]


def test_upload_session_missing_upload_url_returns_list_not_raises():
    routes = _all_green_routes()
    routes[2] = ("createUploadSession", _resp(200, json_body={"unexpected": "shape"}))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[2].ok is False
    assert "uploadUrl" in results[2].detail
    assert [r.ok for r in results[3:]] == [None, None]


def test_upload_put_non_json_response_returns_list_not_raises():
    routes = _all_green_routes()
    routes[3] = ("upload.example", _resp(201, content=b"<html>not json</html>"))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[2].ok is False
    # I2：PUT 已经 201 了，文件字节大概率已经落地，detail 必须带清理线索
    assert "pptx2pdf-selftest-" in results[2].detail
    assert [r.ok for r in results[3:]] == [None, None]


def test_upload_session_non_json_response_returns_list_not_raises():
    # 与 test_upload_session_missing_upload_url_returns_list_not_raises 不同：
    # 那个是合法 JSON 但缺字段，这个是响应体本身就不是 JSON——两条分支
    # 之前都是零覆盖，复审逐个还原成旧写法都全绿通过。
    routes = _all_green_routes()
    routes[2] = ("createUploadSession", _resp(200, content=b"<html>Sign in</html>"))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[2].ok is False
    assert "JSON" in results[2].detail
    assert [r.ok for r in results[3:]] == [None, None]


def test_upload_put_missing_id_returns_list_not_raises():
    # I2 finding 的本体场景：PUT 已经 200/201（文件字节已经落地到租户），
    # 响应是合法 JSON，只是没有 id 字段——不同于「响应根本不是 JSON」。
    routes = _all_green_routes()
    routes[3] = ("upload.example", _resp(201, json_body={"name": "selftest.pptx"}))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[2].ok is False
    assert "pptx2pdf-selftest-" in results[2].detail
    assert [r.ok for r in results[3:]] == [None, None]


# ---- I1: 五个 httpx.HTTPError 网络异常分支此前完全没有测试触达 ----


def test_token_network_error_short_circuits():
    fake = _FakeClient([("oauth2/v2.0/token", _raise(httpx.ConnectError("boom")))])
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[0].ok is False
    assert "ConnectError" in results[0].detail
    assert [r.ok for r in results[1:]] == [None, None, None, None]


def test_drive_network_error_short_circuits():
    routes = _all_green_routes()
    routes[1] = ("/drive", _raise(httpx.ConnectError("boom")))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[0].ok is True
    assert results[1].ok is False
    assert "ConnectError" in results[1].detail
    assert [r.ok for r in results[2:]] == [None, None, None]


def test_upload_network_error_short_circuits():
    routes = _all_green_routes()
    routes[2] = ("createUploadSession", _raise(httpx.ConnectError("boom")))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[2].ok is False
    assert "ConnectError" in results[2].detail
    assert [r.ok for r in results[3:]] == [None, None]


def test_convert_network_error_still_deletes():
    routes = _all_green_routes()
    routes[4] = ("format=pdf", _raise(httpx.ConnectError("boom")))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[3].ok is False
    assert "ConnectError" in results[3].detail
    # convert 是网络异常也必须清理已上传的文件
    assert any("permanentDelete" in c.url for c in fake.calls)


def test_delete_network_error_reports_failure():
    routes = _all_green_routes()
    routes[5] = ("permanentDelete", _raise(httpx.ConnectError("boom")))
    fake = _FakeClient(routes)
    results = run_selftest(CREDS, client_factory=lambda **kw: fake)
    assert results[3].ok is True
    assert results[4].ok is False
    assert "ConnectError" in results[4].detail
