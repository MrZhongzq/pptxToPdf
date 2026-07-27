"""Graph 引擎的纯逻辑单测。

不碰网络、不需要 Azure 凭证——覆盖的是错误响应到项目错误码的映射、
重试判定、退避时长计算、URL 拼装、PDF 输出校验这几类可以用构造出来的假
httpx.Response / 假 client 验证的逻辑。

审查轮之后，"假 client" 的边界比第一版画得更宽：`_request_with_retry` /
`_upload_chunks` / `_create_upload_session` / `_access_token` 这些方法把
client 作为参数接收，用一个不到 30 行、纯 Python、不联网的假 client
（只实现 .request/.post/.delete，回放预先构造好的 httpx.Response）就能
完整验证请求次数、sleep 序列、终态异常类型、正文是否保留——这些都是
我们自己的控制流，不是 Graph 语义，所以可以测、也应该测。真正依赖 Graph
真实响应行为的部分（token 端点的真实返回结构、uploadUrl 的真实格式、
`?format=pdf` 的真实 302/403 行为）仍然留到四期用真实租户验证，见
task-6-report.md。

第二轮复审又把边界往上推了一层：光测被拆出来的子函数不够，`convert()`
自己有没有真的调用它们（把校验结果用上、把 upload_url 存住、把清理
挂在 finally 里）是另一层风险——一行接线被删掉，子函数的测试全绿也发现
不了。`_FakeGraphClient` 就是为此加的：撑起 token/createUploadSession/
分片 PUT/内容 GET/两种清理调用的完整出站记录，配合 monkeypatch
`httpx.Client` 本身，让 `GraphEngine.convert()` 完整跑一遍而不碰真实
网络或 Azure 凭证（凭证是假的，`load_credentials` 只要求解密成功，
不要求内容有效）。
"""
import time
from io import BytesIO
from urllib.parse import quote

import httpx
import pytest
from reportlab.pdfgen import canvas

import app.services.engines.graph as graph_module
from app.config import settings
from app.errors import (
    ConversionFailed,
    ConversionPageMismatch,
    ConversionTimeout,
    EngineUnavailable,
)
from app.services.engines.graph import (
    GRAPH_ROOT,
    TOKEN_SKEW_S,
    UPLOAD_CHUNK,
    UPLOAD_UNIT,
    GraphEngine,
    _content_range_headers,
    _content_url,
    _item_base_url,
    _raise_for_status,
    _retry_wait_seconds,
    _staging_filename,
    _token_is_fresh,
    _upload_session_url,
    _verify_pdf_output,
    _wrap_transport_errors,
)
from app.services.graph_credentials import GraphCredentialData
from app.services.pptx_probe import PptxMeta


def _resp(status_code: int, headers: dict | None = None, body: bytes = b"") -> httpx.Response:
    return httpx.Response(
        status_code,
        headers=headers or {},
        content=body,
        request=httpx.Request("GET", "https://graph.microsoft.com/v1.0/x"),
    )


def _write_pdf(path, pages: int) -> None:
    c = canvas.Canvas(str(path), pagesize=(200, 200))
    for _ in range(pages):
        c.drawString(10, 10, "x")
        c.showPage()
    c.save()


def _pdf_bytes(pages: int) -> bytes:
    """跟 _write_pdf 一样，但直接产出字节——用来当 _FakeGraphClient 的
    content GET 响应体，不需要真的落盘。"""
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(200, 200))
    for _ in range(pages):
        c.drawString(10, 10, "x")
        c.showPage()
    c.save()
    return buf.getvalue()


def _meta(slide_count: int) -> PptxMeta:
    return PptxMeta(
        slide_count=slide_count, slide_width_emu=9144000, slide_height_emu=6858000, fonts=()
    )


class _FakeClient:
    """假 client：只实现 GraphEngine 用到的 .request/.delete，不联网、不
    认识 Graph 语义，只回放预先构造好的响应序列（或抛出预先构造好的传输
    异常）。用来验证我们自己的循环控制流，不是 Graph 的行为。"""

    def __init__(self, responses=None, *, delete_result=None, delete_error=None):
        self._responses = list(responses or [])
        self.calls: list[tuple[str, str, dict]] = []
        self.delete_calls: list[str] = []
        self._delete_result = delete_result
        self._delete_error = delete_error

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)

    def delete(self, url, **kwargs):
        self.delete_calls.append(url)
        if self._delete_error is not None:
            raise self._delete_error
        return self._delete_result


class _FakeGraphClient:
    """`convert()` 级别的假 client：撑起 token / createUploadSession /
    分片 PUT / 内容 GET / 两种清理调用（permanentDelete + 降级 DELETE +
    取消上传会话的 DELETE {uploadUrl}）的完整出站记录，用 URL/方法路由到
    预先配置好的响应。不联网、不认识真实 Graph 语义，只用来验证
    `convert()` 自己有没有真的调用到已经测过的那些子函数——上一轮只测了
    子函数本身，`convert()` 这层接线（有没有调用 _verify_pdf_output、
    有没有把 upload_url 存住、有没有把 deadline 传下去、有没有在
    finally 里挂清理）完全没有回归守护，这个假 client 就是补这道守护
    用的。跟 monkeypatch `httpx.Client` 本身配合，让 convert() 完整跑
    一遍。"""

    def __init__(
        self,
        *,
        token_response=None,
        create_session_response=None,
        chunk_responses=None,
        content_response=None,
        cleanup_response=None,
        cleanup_fallback_response=None,
        cancel_response=None,
    ):
        self.calls: list[tuple[str, str, dict]] = []
        self.token_response = token_response or _resp(
            200, body=b'{"access_token": "tok", "expires_in": 3600}'
        )
        self.create_session_response = create_session_response or _resp(
            201, body=b'{"uploadUrl": "https://upload.example/PREAUTH-SECRET"}'
        )
        self.chunk_responses = list(
            chunk_responses
            if chunk_responses is not None
            else [_resp(201, body=b'{"id": "item-1"}')]
        )
        self.content_response = content_response or _resp(200, body=_pdf_bytes(1))
        self.cleanup_response = cleanup_response or _resp(204)
        self.cleanup_fallback_response = cleanup_fallback_response or _resp(204)
        self.cancel_response = cancel_response or _resp(204)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/oauth2/v2.0/token"):
            return self.token_response
        if url.endswith("/permanentDelete"):
            return self.cleanup_response
        raise AssertionError(f"_FakeGraphClient: 没配置的 POST {url}")

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "POST" and url.endswith(":/createUploadSession"):
            return self.create_session_response
        if method == "PUT":
            return self.chunk_responses.pop(0)
        if method == "GET" and "format=pdf" in url:
            return self.content_response
        raise AssertionError(f"_FakeGraphClient: 没配置的 {method} {url}")

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        if "PREAUTH-SECRET" in url:
            return self.cancel_response
        return self.cleanup_fallback_response


@pytest.fixture
def graph_credentials() -> GraphCredentialData:
    """Task 8 把凭证加载移出了 convert()：GraphEngine 现在直接从构造函数
    接收已经读好的凭证，convert() 级别的测试不再需要搭一套 DB
    （create_all + save_credentials）才能让流程走到 HTTP 编排那一段——
    凭证内容本来就是假的，_FakeGraphClient 也不校验它，直接构造
    GraphCredentialData 就够。"""
    return GraphCredentialData(
        tenant_id="tid",
        client_id="cid",
        client_secret="secret",
        site_id="site-1",
        drive_path="staging",
    )


def _patch_client(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(graph_module.httpx, "Client", lambda *a, **kw: fake_client)


def _clock(*values):
    """返回值序列的假 time.monotonic：用完 values 之后一直重复最后一个，
    避免测试因为多算了一次 monotonic() 调用就 StopIteration。"""
    values = list(values)

    def _next():
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    return _next


@pytest.fixture
def sleep_calls(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(graph_module.time, "sleep", lambda s: calls.append(s))
    return calls


# ---- UPLOAD_CHUNK 必须是 320 KiB 的整数倍 ----


def test_upload_chunk_is_multiple_of_320kib():
    # Graph 的 createUploadSession 硬性要求：非末片的每个分块长度必须是
    # 320 KiB 的整数倍。8*1024*1024（brief 里最初的字面量）并不满足这个
    # 约束（8388608 / 327680 = 25.6），是会在四期真机联调时才暴露的
    # 分块失败（官方文档："Using a fragment size that doesn't divide
    # evenly by 320 KiB results in errors committing some files"）。
    assert UPLOAD_UNIT == 320 * 1024
    assert UPLOAD_CHUNK % UPLOAD_UNIT == 0
    assert UPLOAD_CHUNK > 0


# ---- token 缓存判定 ----


def test_token_is_fresh_when_far_from_expiry():
    now = 1_000_000.0
    assert _token_is_fresh("tok", now + 3600, now) is True


def test_token_not_fresh_when_none():
    assert _token_is_fresh(None, 1_000_000.0 + 3600, 1_000_000.0) is False


def test_token_not_fresh_when_already_expired():
    now = 1_000_000.0
    assert _token_is_fresh("tok", now - 1, now) is False


def test_token_not_fresh_within_skew_margin():
    now = 1_000_000.0
    expires_at = now + TOKEN_SKEW_S - 1
    assert _token_is_fresh("tok", expires_at, now) is False


def test_token_boundary_at_exact_skew_margin_is_not_fresh():
    now = 1_000_000.0
    expires_at = now + TOKEN_SKEW_S
    assert _token_is_fresh("tok", expires_at, now) is False


# ---- 重试与退避判定（单次） ----


def test_429_uses_retry_after_header():
    resp = _resp(429, headers={"Retry-After": "12"})
    assert _retry_wait_seconds(resp.status_code, resp.headers, attempt=0) == 12.0


def test_429_without_retry_after_falls_back_to_exponential():
    resp = _resp(429)
    assert _retry_wait_seconds(resp.status_code, resp.headers, attempt=0) == 2.0
    assert _retry_wait_seconds(resp.status_code, resp.headers, attempt=2) == 8.0


def test_429_with_garbage_retry_after_falls_back_to_exponential():
    resp = _resp(429, headers={"Retry-After": "not-a-number"})
    assert _retry_wait_seconds(resp.status_code, resp.headers, attempt=1) == 4.0


def test_5xx_uses_exponential_backoff_ignoring_retry_after():
    resp = _resp(503)
    assert _retry_wait_seconds(resp.status_code, resp.headers, attempt=0) == 2.0
    assert _retry_wait_seconds(resp.status_code, resp.headers, attempt=3) == 16.0


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_2xx_does_not_retry(status):
    resp = _resp(status)
    assert _retry_wait_seconds(resp.status_code, resp.headers, attempt=0) is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_non_429_4xx_does_not_retry(status):
    resp = _resp(status)
    assert _retry_wait_seconds(resp.status_code, resp.headers, attempt=0) is None


# ---- 重试循环控制流（假 client，不联网） ----


def test_request_with_retry_returns_first_success_without_sleeping(sleep_calls):
    client = _FakeClient([_resp(200)])
    resp = GraphEngine()._request_with_retry(
        client, "GET", "https://x", deadline=time.monotonic() + 100
    )
    assert resp.status_code == 200
    assert len(client.calls) == 1
    assert sleep_calls == []


def test_request_with_retry_retries_5xx_then_succeeds(monkeypatch, sleep_calls):
    monkeypatch.setattr(settings, "graph_max_retries", 3)
    client = _FakeClient([_resp(500), _resp(500), _resp(200)])
    resp = GraphEngine()._request_with_retry(
        client, "GET", "https://x", deadline=time.monotonic() + 1000
    )
    assert resp.status_code == 200
    assert len(client.calls) == 3
    assert sleep_calls == [2.0, 4.0]


def test_request_with_retry_exhausts_retries_raises_engine_unavailable_with_body(
    monkeypatch, sleep_calls
):
    # I4：重试耗尽后是"服务暂时不可用"（EngineUnavailable/503），不是
    # ConversionFailed——用户看到"转换失败"的合理反应是换个文件重传，
    # 而限流期间重传必然再失败，真正该给的是"稍后重试"。
    monkeypatch.setattr(settings, "graph_max_retries", 3)
    client = _FakeClient([_resp(500, body=b"OfficeConversion_Fatal detail")] * 3)
    with pytest.raises(EngineUnavailable) as exc:
        GraphEngine()._request_with_retry(
            client, "GET", "https://x", deadline=time.monotonic() + 1000
        )
    assert len(client.calls) == 3
    # I2 点 3：最后一次尝试后不 sleep——3 次请求只对应 2 次退避等待。
    assert sleep_calls == [2.0, 4.0]
    # I3：正文没有被重试耗尽路径吞掉。
    assert "500" in str(exc.value)
    assert "OfficeConversion_Fatal detail" in str(exc.value)


def test_request_with_retry_gives_up_immediately_when_budget_already_exhausted(
    sleep_calls,
):
    # I1/I2：timeout_s 预算耗尽时不重试直接抛，不把预算耗在必然超时的
    # 等待上——这里预算在发第一个请求之前就已经耗尽，连请求都不该发。
    client = _FakeClient([_resp(500)])
    with pytest.raises(ConversionTimeout):
        GraphEngine()._request_with_retry(
            client, "GET", "https://x", deadline=time.monotonic() - 1
        )
    assert client.calls == []
    assert sleep_calls == []


def test_request_with_retry_gives_up_before_sleep_when_budget_runs_out_mid_loop(
    monkeypatch, sleep_calls
):
    monkeypatch.setattr(settings, "graph_max_retries", 3)
    # 第一次 remaining 检查（发请求前）还有预算，能发出请求；拿到 429
    # 之后、真正 sleep 之前的第二次检查预算已经耗尽——不能真的调
    # time.sleep(600)，必须直接放弃。
    monkeypatch.setattr(graph_module.time, "monotonic", _clock(0.0, 5.0))
    client = _FakeClient([_resp(429, headers={"Retry-After": "600"})])
    with pytest.raises(ConversionTimeout):
        GraphEngine()._request_with_retry(client, "GET", "https://x", deadline=1.0)
    assert len(client.calls) == 1
    assert sleep_calls == []


def test_request_with_retry_gives_up_without_sleeping_when_wait_exceeds_remaining_budget(
    monkeypatch, sleep_calls
):
    # 本轮修的一行 bug：wait >= remaining 时不该先把剩余预算睡光再放弃
    # ——那一觉必然无用（睡完预算肯定是 0，接下来必然放弃）。切片场景下
    # 12 个分片各占一个 worker 空睡几百秒是可观的运力损失。这里剩余预算
    # 5 秒，Retry-After 说 600 秒，正确行为是不睡、直接放弃，不是睡 5 秒
    # 再重试（那是上一轮的旧行为，已被这次审查判定为浪费）。
    monkeypatch.setattr(settings, "graph_max_retries", 3)
    monkeypatch.setattr(graph_module.time, "monotonic", _clock(0.0))
    client = _FakeClient([_resp(429, headers={"Retry-After": "600"})])
    with pytest.raises(ConversionTimeout):
        GraphEngine()._request_with_retry(client, "GET", "https://x", deadline=5.0)
    assert len(client.calls) == 1
    assert sleep_calls == []


def test_request_with_retry_wraps_transport_error_as_engine_unavailable(sleep_calls):
    class _ExplodingClient:
        def request(self, method, url, **kwargs):
            raise httpx.ConnectError("boom")

    with pytest.raises(EngineUnavailable):
        GraphEngine()._request_with_retry(
            _ExplodingClient(), "GET", "https://x", deadline=time.monotonic() + 100
        )


def test_request_with_retry_reraises_timeout_exception_for_caller_to_convert():
    # httpx 的真实超时异常要原样抛出去，让 convert() 转成 ConversionTimeout
    # （现有行为），不能在这里被 HTTPError 的宽异常处理吞掉或错误分类。
    class _TimingOutClient:
        def request(self, method, url, **kwargs):
            raise httpx.ReadTimeout("timeout")

    with pytest.raises(httpx.ReadTimeout):
        GraphEngine()._request_with_retry(
            _TimingOutClient(), "GET", "https://x", deadline=time.monotonic() + 100
        )


# ---- URL / 路径拼装 ----


def test_upload_session_url_joins_drive_path_and_filename():
    url = _upload_session_url("site-1", "pptx2pdf-staging", "deck.pptx")
    assert url == (
        f"{GRAPH_ROOT}/sites/site-1/drive/root:/pptx2pdf-staging/deck.pptx:"
        "/createUploadSession"
    )


def test_upload_session_url_percent_encodes_drive_path_and_filename():
    # M4：drive_path 来自四期管理员配置，含空格/# 会把 URL 截断。
    url = _upload_session_url("site-1", "my staging", "a b#c.pptx")
    assert " " not in url
    assert quote("my staging", safe="/") in url
    assert quote("a b#c.pptx", safe="") in url


def test_content_url_requests_pdf_format():
    url = _content_url("site-1", "item-9")
    assert url == f"{GRAPH_ROOT}/sites/site-1/drive/items/item-9/content?format=pdf"


def test_item_base_url():
    assert _item_base_url("site-1", "item-9") == (
        f"{GRAPH_ROOT}/sites/site-1/drive/items/item-9"
    )


def test_content_range_headers_first_chunk():
    headers = _content_range_headers(offset=0, length=100, total=500)
    assert headers == {"Content-Range": "bytes 0-99/500", "Content-Length": "100"}


def test_content_range_headers_last_chunk():
    headers = _content_range_headers(offset=400, length=100, total=500)
    assert headers == {"Content-Range": "bytes 400-499/500", "Content-Length": "100"}


def test_content_range_headers_single_byte_block():
    headers = _content_range_headers(offset=10, length=1, total=11)
    assert headers["Content-Range"] == "bytes 10-10/11"


# ---- 中转文件名唯一化（I8） ----


def test_staging_filename_is_unique_and_preserves_original_name(tmp_path):
    src = tmp_path / "001.pptx"
    a = _staging_filename(src)
    b = _staging_filename(src)
    assert a != b
    assert a.endswith("001.pptx")
    assert b.endswith("001.pptx")
    assert a != "001.pptx"


# ---- Graph 响应 -> 项目错误码映射 ----


def test_raise_for_status_passes_through_ok_status():
    resp = _resp(201)
    _raise_for_status(resp, frozenset({200, 201}), ConversionFailed, "上传失败")


def test_raise_for_status_raises_configured_error_class():
    resp = _resp(500, body=b"internal error detail")
    with pytest.raises(ConversionFailed) as exc:
        _raise_for_status(resp, frozenset({200}), ConversionFailed, "创建上传会话失败")
    assert "创建上传会话失败" in str(exc.value)
    assert "500" in str(exc.value)
    assert "internal error detail" in str(exc.value)


def test_raise_for_status_uses_caller_supplied_error_class():
    resp = _resp(401, body=b"invalid_client")
    with pytest.raises(EngineUnavailable):
        _raise_for_status(resp, frozenset({200}), EngineUnavailable, "Azure 认证失败")


def test_raise_for_status_truncates_long_body():
    resp = _resp(400, body=b"x" * 500)
    with pytest.raises(ConversionFailed) as exc:
        _raise_for_status(resp, frozenset({200}), ConversionFailed, "失败")
    body_in_message = str(exc.value).split("：", 1)[1]
    assert len(body_in_message) <= 200


def test_wrap_transport_errors_hides_raw_exception_text():
    # I5：httpx 异常消息可能带上预授权 uploadUrl 或其他请求细节，只暴露
    # 异常类型名，足够排障又不会把这些细节泄露到用户可见的 error_message。
    exc = httpx.ConnectError("boom detail with maybe a secret uploadUrl")
    wrapped = _wrap_transport_errors(exc, EngineUnavailable, "Graph 请求失败")
    assert isinstance(wrapped, EngineUnavailable)
    assert "boom detail" not in str(wrapped)
    assert "ConnectError" in str(wrapped)
    assert "Graph 请求失败" in str(wrapped)


# ---- PDF 输出校验（C1） ----


def test_verify_pdf_output_passes_when_pages_match(tmp_path):
    dest = tmp_path / "out.pdf"
    _write_pdf(dest, 3)
    _verify_pdf_output(dest, _meta(3), tmp_path / "src.pptx")  # 不应抛
    assert dest.exists()


def test_verify_pdf_output_raises_page_mismatch_and_deletes_file(tmp_path):
    # 这条防的正是三期存在的理由：Graph 逼近 100 页硬上限时可能返回
    # 200 + 被截断的 PDF，只有页数比对能发现。
    dest = tmp_path / "out.pdf"
    _write_pdf(dest, 2)
    with pytest.raises(ConversionPageMismatch):
        _verify_pdf_output(dest, _meta(5), tmp_path / "src.pptx")
    assert not dest.exists()


def test_verify_pdf_output_raises_when_file_missing(tmp_path):
    with pytest.raises(ConversionFailed):
        _verify_pdf_output(tmp_path / "missing.pdf", _meta(1), tmp_path / "src.pptx")


def test_verify_pdf_output_raises_and_deletes_when_empty(tmp_path):
    dest = tmp_path / "out.pdf"
    dest.write_bytes(b"")
    with pytest.raises(ConversionFailed):
        _verify_pdf_output(dest, _meta(1), tmp_path / "src.pptx")
    assert not dest.exists()


def test_verify_pdf_output_raises_and_deletes_when_unparseable(tmp_path):
    # 302 落到登录页/错误页而不是真 PDF 时，size > 0 但根本不是 PDF。
    dest = tmp_path / "out.pdf"
    dest.write_bytes(b"<html>not a pdf, e.g. a login page</html>")
    with pytest.raises(ConversionFailed):
        _verify_pdf_output(dest, _meta(1), tmp_path / "src.pptx")
    assert not dest.exists()


# ---- _access_token（假 client，不联网） ----


def test_access_token_returns_cached_token_without_calling_client():
    engine = GraphEngine()
    engine._token = "cached"
    engine._token_expires_at = time.time() + 3600

    class _ExplodingClient:
        def post(self, *a, **kw):
            raise AssertionError("不该调用 client.post——token 还新鲜")

    token = engine._access_token(_ExplodingClient(), "tid", "cid", "secret")
    assert token == "cached"


def test_access_token_fetches_and_caches_new_token():
    class _StubClient:
        def post(self, url, **kwargs):
            return _resp(200, body=b'{"access_token": "tok-1", "expires_in": 3600}')

    engine = GraphEngine()
    token = engine._access_token(_StubClient(), "tid", "cid", "secret")
    assert token == "tok-1"
    assert engine._token == "tok-1"


def test_access_token_raises_engine_unavailable_on_non_200():
    class _StubClient:
        def post(self, url, **kwargs):
            return _resp(401, body=b"invalid_client")

    with pytest.raises(EngineUnavailable):
        GraphEngine()._access_token(_StubClient(), "tid", "cid", "secret")


def test_access_token_wraps_transport_error_as_engine_unavailable():
    # I5：DNS 失败/代理断连/网络抖动都是高频事件，不能让裸 httpx 异常
    # 穿到 pipeline 的兜底变成 INTERNAL_ERROR。
    class _StubClient:
        def post(self, url, **kwargs):
            raise httpx.ReadTimeout("timeout")

    with pytest.raises(EngineUnavailable):
        GraphEngine()._access_token(_StubClient(), "tid", "cid", "secret")


# ---- _create_upload_session / _upload_chunks（假 client，不联网） ----


def test_create_upload_session_returns_upload_url_and_uses_unique_staging_name(
    tmp_path,
):
    src = tmp_path / "001.pptx"
    src.write_bytes(b"x")
    client = _FakeClient([_resp(201, body=b'{"uploadUrl": "https://upload.example/abc"}')])
    url = GraphEngine()._create_upload_session(
        client,
        {"Authorization": "Bearer t"},
        "site-1",
        "staging",
        src,
        deadline=time.monotonic() + 100,
    )
    assert url == "https://upload.example/abc"
    method, called_url, _kwargs = client.calls[0]
    assert method == "POST"
    assert "/sites/site-1/drive/root:/staging/" in called_url
    # I8：中转文件名带唯一前缀，不是裸的 src.name。
    assert called_url != _upload_session_url("site-1", "staging", "001.pptx")
    assert called_url.endswith("001.pptx:/createUploadSession")


def test_upload_chunks_advances_offset_and_returns_item_id_on_final_chunk(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(graph_module, "UPLOAD_CHUNK", 4)
    src = tmp_path / "deck.pptx"
    src.write_bytes(b"0123456789")  # 10 字节 -> 4/4/2 三块
    client = _FakeClient(
        [
            _resp(202),
            _resp(202),
            _resp(201, body=b'{"id": "item-42"}'),
        ]
    )
    item_id = GraphEngine()._upload_chunks(
        client, "https://upload.example/abc", src, deadline=time.monotonic() + 100
    )
    assert item_id == "item-42"
    assert len(client.calls) == 3
    ranges = [kwargs["headers"]["Content-Range"] for _, _, kwargs in client.calls]
    assert ranges == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]


def test_upload_chunks_raises_immediately_for_zero_byte_source(tmp_path):
    # M5：0 字节源文件此前会报"上传完成但未返回 driveItem id"，
    # 跟真实原因（while 循环一次都没进）对不上，误导排障。
    src = tmp_path / "empty.pptx"
    src.write_bytes(b"")
    client = _FakeClient([])
    with pytest.raises(ConversionFailed) as exc:
        GraphEngine()._upload_chunks(
            client, "https://upload.example/abc", src, deadline=time.monotonic() + 100
        )
    assert client.calls == []
    assert "0 字节" in str(exc.value)


def test_upload_chunks_propagates_failure_so_caller_can_cancel_session(tmp_path):
    src = tmp_path / "deck.pptx"
    src.write_bytes(b"0123456789")
    client = _FakeClient([_resp(403, body=b"forbidden")])
    with pytest.raises(ConversionFailed):
        GraphEngine()._upload_chunks(
            client, "https://upload.example/abc", src, deadline=time.monotonic() + 100
        )


def test_upload_chunks_retries_5xx_on_a_single_chunk(tmp_path, monkeypatch, sleep_calls):
    # I7：分块 PUT 现在也走 _request_with_retry，一次 502 不该让整个
    # 40MB 分片的转换直接失败。范围裁决只做重试（重传整块），不做断点
    # 续传——见 I7 的裁决说明。
    monkeypatch.setattr(graph_module, "UPLOAD_CHUNK", 10)
    monkeypatch.setattr(settings, "graph_max_retries", 3)
    src = tmp_path / "deck.pptx"
    src.write_bytes(b"0123456789")  # 10 字节，一块打完
    client = _FakeClient([_resp(502), _resp(201, body=b'{"id": "item-9"}')])
    item_id = GraphEngine()._upload_chunks(
        client, "https://upload.example/abc", src, deadline=time.monotonic() + 100
    )
    assert item_id == "item-9"
    assert len(client.calls) == 2
    assert sleep_calls == [2.0]


# ---- _cancel_upload_session（C2，假 client，不联网） ----


def test_cancel_upload_session_calls_delete_on_upload_url():
    client = _FakeClient(delete_result=_resp(204))
    GraphEngine()._cancel_upload_session(client, "https://upload.example/abc")
    assert client.delete_calls == ["https://upload.example/abc"]


def test_cancel_upload_session_swallows_transport_errors():
    # 清理失败不能掩盖原始的转换失败原因——合并模块踩过这个坑。
    client = _FakeClient(delete_error=httpx.ConnectError("boom"))
    GraphEngine()._cancel_upload_session(client, "https://upload.example/abc")  # 不应抛


# ---- convert() 在凭证缺失时的行为：不碰 HTTP 就能验证 ----
#
# "用户没配置 Graph 凭证" 这条路径（GraphNotConfigured）已经不再是
# GraphEngine.convert() 的责任——Task 8 把凭证加载移到了
# app.services.engines.get_engine()，那里的 load_credentials(session)
# 才是真正判定"配没配置"的地方，覆盖见 tests/test_engines_registry.py
# 和 tests/test_graph_credentials.py。这里只需要守住 GraphEngine 自己的
# 那一半契约：如果调用方绕开 get_engine 直接构造实例又忘了注入凭证，
# 必须是一个说得清楚原因的错误，不是访问 None.tenant_id 时的 AttributeError。


def test_convert_raises_clear_error_when_constructed_without_credentials(tmp_path):
    """正常路径下这走不到——get_engine() 总是先注入凭证再返回实例。
    这里测的是"万一被绕过"：必须是可读的 RuntimeError，而不是
    None.tenant_id 这种远离真实原因的 AttributeError。"""
    engine = GraphEngine()  # 没有注入凭证
    with pytest.raises(RuntimeError, match="credentials"):
        engine.convert(
            tmp_path / "deck.pptx",
            _meta(1),
            tmp_path / "out.pdf",
            timeout_s=50,
        )


# ---- convert() 接线层守护：子函数测过不等于 convert() 真的调用了它们 ----


def test_convert_wires_output_verification_rejects_html_as_pdf(
    tmp_path, monkeypatch, graph_credentials, sleep_calls
):
    """C1 的接线守护：如果 convert() 里 `_verify_pdf_output(dest, meta,
    src)` 这行调用被删掉，Graph 返回的 HTML（302 落到登录页时的典型
    形状）会被原样写成 .pdf 然后被当成转换成功——这里必须变红。"""
    fake = _FakeGraphClient(
        content_response=_resp(200, body=b"<html>login page, not a pdf</html>")
    )
    _patch_client(monkeypatch, fake)

    src = tmp_path / "deck.pptx"
    src.write_bytes(b"pptx bytes")
    dest = tmp_path / "out.pdf"

    with pytest.raises(ConversionFailed):
        GraphEngine(graph_credentials).convert(src, _meta(1), dest, timeout_s=50)
    assert not dest.exists()


def test_convert_wires_output_verification_rejects_page_count_mismatch(
    tmp_path, monkeypatch, graph_credentials, sleep_calls
):
    """同上，另一种故障形状：返回的是真 PDF，但页数比 meta.slide_count
    少（Graph 逼近 100 页硬上限时的典型截断）。"""
    fake = _FakeGraphClient(content_response=_resp(200, body=_pdf_bytes(2)))
    _patch_client(monkeypatch, fake)

    src = tmp_path / "deck.pptx"
    src.write_bytes(b"pptx bytes")
    dest = tmp_path / "out.pdf"

    with pytest.raises(ConversionPageMismatch):
        GraphEngine(graph_credentials).convert(src, _meta(5), dest, timeout_s=50)
    assert not dest.exists()


def test_convert_wires_cancel_upload_session_when_a_chunk_fails_midway(
    tmp_path, monkeypatch, graph_credentials, sleep_calls
):
    """C2 的接线守护：如果 convert() 的 finally 里
    `elif upload_url: self._cancel_upload_session(...)` 被删掉，这里
    必须变红——直接断言出站请求里真的有 DELETE {uploadUrl}，不是只信
    _upload_chunks/_cancel_upload_session 各自的单测。"""
    monkeypatch.setattr(graph_module, "UPLOAD_CHUNK", 4)
    fake = _FakeGraphClient(
        chunk_responses=[_resp(202), _resp(403, body=b"forbidden")]
    )
    _patch_client(monkeypatch, fake)

    src = tmp_path / "deck.pptx"
    src.write_bytes(b"01234567")  # 8 字节 -> 两块 4+4，第二块失败
    dest = tmp_path / "out.pdf"

    with pytest.raises(ConversionFailed):
        GraphEngine(graph_credentials).convert(src, _meta(1), dest, timeout_s=50)

    delete_calls = [url for method, url, _ in fake.calls if method == "DELETE"]
    assert any("PREAUTH-SECRET" in url for url in delete_calls), (
        f"应该有 DELETE {{uploadUrl}} 取消上传会话，实际出站请求: {fake.calls}"
    )


def test_convert_treats_timeout_s_as_wall_clock_budget(
    tmp_path, monkeypatch, graph_credentials, sleep_calls
):
    """I1/I2 的接线守护：如果 convert() 里 `deadline = started +
    timeout_s` 被改回"退回无预算"（比如 `started + 1e9`），这里必须
    变红——用很小的 timeout_s 配合一个持续 429/Retry-After:600 的内容
    GET 响应，断言总耗时被 timeout_s 死死摁住，不是照着 Retry-After
    睡了一大截。跟本轮同时修的一行 bug（wait>=remaining 时不睡）叠加，
    正确实现应该是毫秒级返回，不是"睡到 timeout_s 那一刻才放弃"。"""
    fake = _FakeGraphClient(
        content_response=_resp(429, headers={"Retry-After": "600"})
    )
    _patch_client(monkeypatch, fake)

    src = tmp_path / "deck.pptx"
    src.write_bytes(b"pptx bytes")
    dest = tmp_path / "out.pdf"

    started = time.monotonic()
    with pytest.raises(ConversionTimeout):
        GraphEngine(graph_credentials).convert(src, _meta(1), dest, timeout_s=3)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"耗时 {elapsed:.2f}s——预算该在毫秒级内放弃，不是睡到快超时"
    assert sleep_calls == []


def test_convert_wires_cleanup_of_transit_file_on_success(
    tmp_path, monkeypatch, graph_credentials, sleep_calls
):
    """如果 convert() 的 finally 里 `self._cleanup(...)` 调用被删掉，
    成功路径上传到 SharePoint 中转库的 pptx 会永远留在共享站点里——
    这里必须变红：断言出站请求里真的有 permanentDelete。"""
    fake = _FakeGraphClient()
    _patch_client(monkeypatch, fake)

    src = tmp_path / "deck.pptx"
    src.write_bytes(b"pptx bytes")
    dest = tmp_path / "out.pdf"

    GraphEngine(graph_credentials).convert(src, _meta(1), dest, timeout_s=50)

    assert dest.exists()
    cleanup_calls = [
        (method, url)
        for method, url, _ in fake.calls
        if method == "POST" and url.endswith("/permanentDelete")
    ]
    assert len(cleanup_calls) == 1, f"应该恰好清理一次，实际出站请求: {fake.calls}"
