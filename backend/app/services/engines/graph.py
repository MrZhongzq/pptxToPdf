import logging
import time
from pathlib import Path
from typing import Mapping

import httpx

from app.config import settings
from app.db import SessionLocal
from app.errors import (
    AppError,
    ConversionFailed,
    ConversionTimeout,
    EngineUnavailable,
)
from app.services.engines.base import ConversionEngine
from app.services.graph_credentials import load_credentials
from app.services.pptx_probe import PptxMeta

logger = logging.getLogger(__name__)

LOGIN_HOST = "https://login.microsoftonline.com"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

UPLOAD_UNIT = 320 * 1024
"""createUploadSession 分片大小的基本单位。Graph 硬性要求：除最后一片外，
每个分片的字节数必须是 320 KiB 的整数倍——不满足会在真实上传时报错。"""
UPLOAD_CHUNK = 32 * UPLOAD_UNIT
"""10 MiB，是 UPLOAD_UNIT 的整数倍（32 * 320 KiB），见
test_upload_chunk_is_multiple_of_320kib。"""
TOKEN_SKEW_S = 300


# ---- 纯逻辑：不碰网络，构造一个 httpx.Response/dict 就能验证 ----


def _token_is_fresh(token: str | None, expires_at: float, now: float) -> bool:
    """token 缓存判定。留 TOKEN_SKEW_S 秒安全边际，避免拿着一个请求发出去
    就可能过期的 token 起飞。"""
    return bool(token) and now < expires_at - TOKEN_SKEW_S


def _retry_wait_seconds(
    status_code: int, headers: Mapping[str, str], attempt: int
) -> float | None:
    """决定这个状态码要不要重试、重试前等多久。返回 None 表示不重试。

    429 优先用 Retry-After 头（缺失或不是数字就退避 2**(attempt+1) 秒）；
    5xx 一律用同样的指数退避；其余状态码（含 2xx 与非 429 的 4xx）是
    请求本身的问题，重试没有意义，直接放行让调用方处理。
    """
    if status_code == 429:
        raw = headers.get("Retry-After")
        if raw is not None:
            try:
                return float(raw)
            except ValueError:
                pass
        return float(2 ** (attempt + 1))
    if 500 <= status_code < 600:
        return float(2 ** (attempt + 1))
    return None


def _upload_session_url(site_id: str, drive_path: str, filename: str) -> str:
    return (
        f"{GRAPH_ROOT}/sites/{site_id}/drive/root:/{drive_path}/{filename}:"
        "/createUploadSession"
    )


def _content_url(site_id: str, item_id: str) -> str:
    return f"{GRAPH_ROOT}/sites/{site_id}/drive/items/{item_id}/content?format=pdf"


def _item_base_url(site_id: str, item_id: str) -> str:
    return f"{GRAPH_ROOT}/sites/{site_id}/drive/items/{item_id}"


def _content_range_headers(offset: int, length: int, total: int) -> dict[str, str]:
    """createUploadSession 分片 PUT 用的 Content-Range/Content-Length。"""
    end = offset + length - 1
    return {
        "Content-Range": f"bytes {offset}-{end}/{total}",
        "Content-Length": str(length),
    }


def _raise_for_status(
    resp: httpx.Response,
    ok_statuses: frozenset[int],
    error_cls: type[AppError],
    action: str,
) -> None:
    """把非成功状态码映射成项目错误码。

    不同调用点的成功状态码集合、该报的异常类型都不一样——认证失败是
    EngineUnavailable，创建上传会话 / 上传分块 / 下载转换失败是
    ConversionFailed；createUploadSession 认 200/201，分片 PUT 还认 202
    （分片未收满），下载只认 200。这里只做纯粹的判断和消息拼装，
    不发请求、不重试。
    """
    if resp.status_code in ok_statuses:
        return
    raise error_cls(f"{action}（HTTP {resp.status_code}）：{resp.text[:200]}")


class GraphEngine(ConversionEngine):
    """Microsoft Graph 转换引擎。

    流程：拿 token -> 上传分片到 SharePoint 中转库 -> ?format=pdf 转换
    -> 下载 -> 清理中转文件。

    每次转换新建实例（get_engine 的惰性构造），token 缓存只在单次转换内
    有效。切片场景下每个分片是独立 RQ job、跑在 fork 的子进程里，12 片
    就是 12 次 token 请求——token 端点响应在百毫秒级，相比每片几十秒的
    转换可以忽略，而共享 token 要引入 Redis 级缓存，等于为省几次快请求
    新增一个密钥存储面。不做。

    本类里发真实 HTTP 请求的编排代码（_access_token/_upload/_cleanup/
    convert 里调 client.request 的部分）没有单测——它们需要真实 Azure
    租户才能验证，mock 出来的 HTTP 往返只会测到 mock 本身。已经从这些
    编排代码里拆出来的纯判断/拼装逻辑（token 缓存判定、重试与退避、
    URL 拼装、响应到错误码的映射）都在 tests/test_graph_engine.py 里
    覆盖，不需要凭证就能跑。四期拿到真实租户凭证后的验证计划见
    task-6-report.md。
    """

    name = "graph"

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ---- 认证 ----

    def _access_token(
        self, client: httpx.Client, tenant_id: str, client_id: str, client_secret: str
    ) -> str:
        if _token_is_fresh(self._token, self._token_expires_at, time.time()):
            assert self._token is not None
            return self._token

        resp = client.post(
            f"{LOGIN_HOST}/{tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        _raise_for_status(resp, frozenset({200}), EngineUnavailable, "Azure 认证失败")
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    # ---- 重试 ----

    def _request_with_retry(
        self, client: httpx.Client, method: str, url: str, **kwargs
    ) -> httpx.Response:
        """429 与 5xx 退避重试；4xx（除 429）立即失败——那是请求本身的
        问题，重试无意义。退避判定本身在 _retry_wait_seconds，这里只管
        发请求、按判定结果 sleep、耗尽重试后报错。"""
        last: httpx.Response | None = None
        for attempt in range(settings.graph_max_retries):
            resp = client.request(method, url, **kwargs)
            wait = _retry_wait_seconds(resp.status_code, resp.headers, attempt)
            if wait is None:
                return resp
            logger.warning(
                "Graph %d，%.0f 秒后重试（第 %d 次）", resp.status_code, wait, attempt + 1
            )
            time.sleep(wait)
            last = resp
        raise ConversionFailed(
            f"Graph 请求重试 {settings.graph_max_retries} 次后仍失败："
            f"HTTP {last.status_code if last else '?'}"
        )

    # ---- 上传 ----

    def _upload(
        self,
        client: httpx.Client,
        headers: dict,
        site_id: str,
        drive_path: str,
        src: Path,
    ) -> str:
        """用 createUploadSession 分片上传，返回 driveItem id。

        小于 4MB 的文件可以直接 PUT content，但分片是 ≤40MB，一律走
        upload session 省掉一条分支。
        """
        resp = self._request_with_retry(
            client,
            "POST",
            _upload_session_url(site_id, drive_path, src.name),
            headers=headers,
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        _raise_for_status(resp, frozenset({200, 201}), ConversionFailed, "创建上传会话失败")
        upload_url = resp.json()["uploadUrl"]

        size = src.stat().st_size
        with src.open("rb") as fh:
            offset = 0
            while offset < size:
                block = fh.read(UPLOAD_CHUNK)
                end = offset + len(block) - 1
                put = client.put(
                    upload_url,
                    content=block,
                    headers=_content_range_headers(offset, len(block), size),
                )
                _raise_for_status(
                    put,
                    frozenset({200, 201, 202}),
                    ConversionFailed,
                    f"上传分块 {offset}-{end} 失败",
                )
                offset = end + 1
                if put.status_code in (200, 201):
                    return put.json()["id"]
        raise ConversionFailed("上传完成但未返回 driveItem id")

    # ---- 清理 ----

    def _cleanup(
        self, client: httpx.Client, headers: dict, site_id: str, item_id: str
    ) -> None:
        """先试 permanentDelete，403 则退回普通 DELETE 并告警。

        permanentDelete 文档标注的应用权限是 Files.ReadWrite.All /
        Sites.ReadWrite.All，与设计里 Sites.Selected 的最小权限意图冲突；
        社区另有报告称它有时仍把文件送进回收站。三期无法实测，四期配上
        凭证后按实际返回收敛。

        清理失败不影响转换结果——文件已经转好了，中转残留是运维问题。
        但必须记日志：两级回收站仍占 SharePoint 配额，悄悄吃满很难查。
        """
        base = _item_base_url(site_id, item_id)
        try:
            resp = client.post(f"{base}/permanentDelete", headers=headers)
            if resp.status_code in (200, 204):
                return
            if resp.status_code == 403:
                logger.warning(
                    "permanentDelete 权限不足（403），退回普通 DELETE。"
                    "中转文件会进回收站并占用配额，考虑改授 Sites.ReadWrite.All"
                )
            else:
                logger.warning(
                    "permanentDelete 返回 %d，退回普通 DELETE", resp.status_code
                )
            fallback = client.delete(base, headers=headers)
            if fallback.status_code not in (200, 204):
                logger.error(
                    "中转文件清理失败 item=%s HTTP %d", item_id, fallback.status_code
                )
        except httpx.HTTPError as exc:
            logger.error("中转文件清理异常 item=%s: %s", item_id, exc)

    # ---- 主流程 ----

    def convert(
        self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float
    ) -> None:
        session = SessionLocal()
        try:
            creds = load_credentials(session)
        finally:
            session.close()

        dest.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        item_id: str | None = None

        with httpx.Client(
            timeout=settings.graph_request_timeout_s, follow_redirects=True
        ) as client:
            token = self._access_token(
                client, creds.tenant_id, creds.client_id, creds.client_secret
            )
            headers = {"Authorization": f"Bearer {token}"}

            try:
                logger.info(
                    "graph convert start src=%s slides=%d size=%.1fMB",
                    src.name, meta.slide_count, src.stat().st_size / 1024 / 1024,
                )
                item_id = self._upload(
                    client, headers, creds.site_id, creds.drive_path, src
                )

                try:
                    # follow_redirects=True：?format=pdf 返回 302，Location 是
                    # 几分钟内有效的预授权 URL。不跟随就只能拿到空响应体。
                    resp = self._request_with_retry(
                        client,
                        "GET",
                        _content_url(creds.site_id, item_id),
                        headers=headers,
                    )
                except httpx.TimeoutException as exc:
                    raise ConversionTimeout(
                        f"Graph 转换超时（{settings.graph_request_timeout_s} 秒）。"
                        f"Graph 自身有约 45 秒硬超时，该文件可能过于复杂"
                    ) from exc

                _raise_for_status(resp, frozenset({200}), ConversionFailed, "Graph 转换失败")
                dest.write_bytes(resp.content)
            finally:
                if item_id:
                    self._cleanup(client, headers, creds.site_id, item_id)

        if not dest.is_file() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise ConversionFailed("Graph 返回了空的 PDF")

        logger.info(
            "graph convert done src=%s elapsed=%.1fs output=%.1fMB",
            src.name, time.monotonic() - started,
            dest.stat().st_size / 1024 / 1024,
        )
