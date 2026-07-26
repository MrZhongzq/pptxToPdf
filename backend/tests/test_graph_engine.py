"""Graph 引擎的纯逻辑单测。

不碰网络、不需要 Azure 凭证——覆盖的是错误响应到项目错误码的映射、
重试判定、退避时长计算、URL 拼装这几类可以用构造出来的假 httpx.Response
验证的逻辑。真正发请求的编排代码（_access_token/_upload/_cleanup/convert
里调 client.request 的部分）留到四期用真实租户验证，见任务报告。
"""
import httpx
import pytest

from app.config import settings
from app.errors import ConversionFailed, EngineUnavailable, GraphNotConfigured
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
    _token_is_fresh,
    _upload_session_url,
)


def _resp(status_code: int, headers: dict | None = None, body: bytes = b"") -> httpx.Response:
    return httpx.Response(
        status_code,
        headers=headers or {},
        content=body,
        request=httpx.Request("GET", "https://graph.microsoft.com/v1.0/x"),
    )


# ---- UPLOAD_CHUNK 必须是 320 KiB 的整数倍 ----


def test_upload_chunk_is_multiple_of_320kib():
    # Graph 的 createUploadSession 硬性要求：非末片的每个分块长度必须是
    # 320 KiB 的整数倍。8*1024*1024（brief 里最初的字面量）并不满足这个
    # 约束（8388608 / 327680 = 25.6），是会在四期真机联调时才暴露的
    # 分块失败。用不变量测试挡住任何未来改回不合规值的回归。
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
    # 还没真正过期，但已经进了 TOKEN_SKEW_S 的安全边际——同样要判定为
    # 不新鲜，否则可能拿着一个请求发出去就过期的 token 起飞。
    now = 1_000_000.0
    expires_at = now + TOKEN_SKEW_S - 1
    assert _token_is_fresh("tok", expires_at, now) is False


def test_token_boundary_at_exact_skew_margin_is_not_fresh():
    now = 1_000_000.0
    expires_at = now + TOKEN_SKEW_S
    # 严格小于判定：now < expires_at - TOKEN_SKEW_S ==> now < now，False
    assert _token_is_fresh("tok", expires_at, now) is False


# ---- 重试与退避判定 ----


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
    # 4xx（除 429）是请求本身的问题——重试无意义，立即失败。
    resp = _resp(status)
    assert _retry_wait_seconds(resp.status_code, resp.headers, attempt=0) is None


# ---- URL / 路径拼装 ----


def test_upload_session_url_joins_drive_path_and_filename():
    url = _upload_session_url("site-1", "pptx2pdf-staging", "deck.pptx")
    assert url == (
        f"{GRAPH_ROOT}/sites/site-1/drive/root:/pptx2pdf-staging/deck.pptx:"
        "/createUploadSession"
    )


def test_content_url_requests_pdf_format():
    url = _content_url("site-1", "item-9")
    assert url == f"{GRAPH_ROOT}/sites/site-1/drive/items/item-9/content?format=pdf"


def test_item_base_url():
    assert _item_base_url("site-1", "item-9") == (
        f"{GRAPH_ROOT}/sites/site-1/drive/items/item-9"
    )


def test_content_range_headers_first_chunk():
    headers = _content_range_headers(offset=0, length=100, total=500)
    assert headers == {
        "Content-Range": "bytes 0-99/500",
        "Content-Length": "100",
    }


def test_content_range_headers_last_chunk():
    headers = _content_range_headers(offset=400, length=100, total=500)
    assert headers == {
        "Content-Range": "bytes 400-499/500",
        "Content-Length": "100",
    }


def test_content_range_headers_single_byte_block():
    headers = _content_range_headers(offset=10, length=1, total=11)
    assert headers["Content-Range"] == "bytes 10-10/11"


# ---- Graph 响应 -> 项目错误码映射 ----


def test_raise_for_status_passes_through_ok_status():
    resp = _resp(201)
    _raise_for_status(resp, frozenset({200, 201}), ConversionFailed, "上传失败")  # 不应抛


def test_raise_for_status_raises_configured_error_class():
    resp = _resp(500, body=b"internal error detail")
    with pytest.raises(ConversionFailed) as exc:
        _raise_for_status(resp, frozenset({200}), ConversionFailed, "创建上传会话失败")
    assert "创建上传会话失败" in str(exc.value)
    assert "500" in str(exc.value)
    assert "internal error detail" in str(exc.value)


def test_raise_for_status_uses_caller_supplied_error_class():
    # 认证失败要落 EngineUnavailable，不是 ConversionFailed——不同调用点
    # 语义不同，_raise_for_status 必须尊重调用方传入的异常类型。
    resp = _resp(401, body=b"invalid_client")
    with pytest.raises(EngineUnavailable):
        _raise_for_status(resp, frozenset({200}), EngineUnavailable, "Azure 认证失败")


def test_raise_for_status_truncates_long_body():
    resp = _resp(400, body=b"x" * 500)
    with pytest.raises(ConversionFailed) as exc:
        _raise_for_status(resp, frozenset({200}), ConversionFailed, "失败")
    # brief 的参考实现里各处都是 resp.text[:200]，这里断言截断确实生效，
    # 不会把整段 500 字节的响应体糊进异常消息。
    body_in_message = str(exc.value).split("：", 1)[1]
    assert len(body_in_message) <= 200


# ---- convert() 在凭证缺失时的行为：不碰 HTTP 就能验证 ----


def test_convert_raises_graph_not_configured_without_touching_http(
    tmp_path, monkeypatch
):
    """没配置凭证时，convert 必须在建 httpx.Client 之前就因
    load_credentials 抛 GraphNotConfigured——这条路径完全不涉及网络，
    可以在没有 Azure 账号的机器上跑。"""
    import app.services.engines.graph as graph_module
    from app.services.pptx_probe import PptxMeta

    monkeypatch.setattr(settings, "secret_key", None)

    meta = PptxMeta(
        slide_count=1, slide_width_emu=9144000, slide_height_emu=6858000, fonts=()
    )
    engine = graph_module.GraphEngine()
    with pytest.raises(GraphNotConfigured):
        engine.convert(
            tmp_path / "deck.pptx",
            meta,
            tmp_path / "out.pdf",
            timeout_s=50,
        )


def test_convert_raises_graph_not_configured_when_no_row_saved(
    tmp_path, monkeypatch
):
    """密钥配好了，但管理页面还没填过凭证行——同样要在碰 HTTP 之前失败。
    这条路径比上一条更深：secret_key 检查会过，得真的查一次
    GraphCredential 表才会发现没有行。用来确认 conftest 里对
    app.services.engines.graph.SessionLocal 的隔离 patch 真的生效——
    没生效的话这里查的就是开发者本机的 pptx2pdf.db。"""
    import app.services.engines.graph as graph_module
    from app.services.pptx_probe import PptxMeta

    monkeypatch.setattr(
        settings, "secret_key", "8I3F3CqPwlEsmMDLbEIVSXd8oXlmqkOMWFnDPbNXKvA="
    )

    # 需要真的执行一次 SELECT 才会发现没有凭证行，所以这条路径必须有
    # 建好表的库——conftest 的 autouse 隔离 fixture 只重定向了引擎，没建表
    # （那是给 API 级测试用的，表由 FastAPI 启动事件建）。这里单独建一个
    # 有表的 sqlite 库，再把 graph_module.SessionLocal 指过去。
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base

    iso_engine = create_engine(f"sqlite:///{tmp_path / 'iso.db'}")
    Base.metadata.create_all(iso_engine)
    monkeypatch.setattr(
        graph_module, "SessionLocal", sessionmaker(bind=iso_engine, expire_on_commit=False)
    )

    meta = PptxMeta(
        slide_count=1, slide_width_emu=9144000, slide_height_emu=6858000, fonts=()
    )
    engine = graph_module.GraphEngine()
    with pytest.raises(GraphNotConfigured):
        engine.convert(
            tmp_path / "deck.pptx",
            meta,
            tmp_path / "out.pdf",
            timeout_s=50,
        )
