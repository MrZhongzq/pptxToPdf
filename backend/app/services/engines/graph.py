import logging
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import httpx
from pypdf import PdfReader

from app.config import settings
from app.db import SessionLocal
from app.errors import (
    AppError,
    ConversionFailed,
    ConversionPageMismatch,
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
每个分片的字节数必须是 320 KiB 的整数倍——不满足会在提交阶段才报错
（官方文档："Using a fragment size that doesn't divide evenly by 320 KiB
results in errors committing some files"），最后一块传完才炸，而分片上限
40MB 意味着切片路径下多块是常态。"""
UPLOAD_CHUNK = 32 * UPLOAD_UNIT
"""10 MiB，是 UPLOAD_UNIT 的整数倍（32 * 320 KiB），也命中官方文档
Best practices 推荐的分片粒度。见 test_upload_chunk_is_multiple_of_320kib。"""
TOKEN_SKEW_S = 300


# ---- 纯逻辑：不碰网络，构造一个 httpx.Response/假 client 就能验证 ----


def _token_is_fresh(token: str | None, expires_at: float, now: float) -> bool:
    """token 缓存判定。留 TOKEN_SKEW_S 秒安全边际，避免拿着一个请求发出去
    就可能过期的 token 起飞。"""
    return bool(token) and now < expires_at - TOKEN_SKEW_S


def _retry_wait_seconds(
    status_code: int, headers: Mapping[str, str], attempt: int
) -> float | None:
    """决定这个状态码要不要重试、重试前建议等多久（未考虑总预算，预算
    判断在 _request_with_retry 里）。返回 None 表示不重试。

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
    # drive_path 来自四期管理员配置，filename 来自用户上传的原始文件名
    # （或 pptx_split 生成的分片名）——都可能含空格/# 等会截断 URL 的字符，
    # 必须百分号编码。drive_path 允许含 "/"（多级子目录），filename 不应该
    # 含 "/"，safe="" 更严格地全量编码。
    encoded_path = quote(drive_path, safe="/")
    encoded_name = quote(filename, safe="")
    return (
        f"{GRAPH_ROOT}/sites/{site_id}/drive/root:/{encoded_path}/{encoded_name}:"
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


def _staging_filename(src: Path) -> str:
    """中转文件名加唯一前缀。pptx_split 产出的分片名是 001.pptx/002.pptx
    这类，drive_path 又是全局单值的扁平目录：两个并发任务的 001.pptx 会
    落到同一个路径，conflictBehavior=replace 让后到的覆盖先到的，先到的
    任务随后转的就是别人的课件内容。引擎本身不接收任务上下文（只有 src
    路径），用 uuid4 不需要额外传参就能保证跨任务唯一。"""
    return f"{uuid.uuid4().hex}-{src.name}"


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


def _wrap_transport_errors(
    exc: httpx.HTTPError, error_cls: type[AppError], action: str
) -> AppError:
    """把 httpx 的传输层异常（连接失败、DNS 失败、协议错误等）转成项目
    错误码。不把 str(exc) 塞进消息——它可能带上预授权的 uploadUrl 或者
    其他请求细节，只暴露异常类型名，足够排障又不会泄露。"""
    return error_cls(f"{action}（{type(exc).__name__}）")


def _verify_pdf_output(dest: Path, meta: PptxMeta, src: Path) -> None:
    """成功判定的四条实证，与 libreoffice.py 的 _verify_output 同构：
    存在 / 非 0 字节 / 可解析 / 页数与 meta.slide_count 一致。

    只做"存在且非 0 字节"防不住两类真实故障：
    1. follow_redirects=True 的 302 若落到一个返回 200 的登录页/错误页，
       HTML 会被原样写成 .pdf，size > 0 照样通过。
    2. Graph 在逼近 100 页硬上限时可能返回 200 + 被截断的 PDF——防住这个
       正是三期存在的理由，只有页数比对能发现。
    """
    if not dest.is_file():
        raise ConversionFailed(f"Graph 未产出输出文件: {dest.name}")
    if dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise ConversionFailed("Graph 返回了空的 PDF")
    try:
        pages = len(PdfReader(str(dest)).pages)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise ConversionFailed(f"Graph 返回的 PDF 无法解析: {exc}") from exc
    if pages != meta.slide_count:
        dest.unlink(missing_ok=True)
        raise ConversionPageMismatch(
            f"页数不符：{src.name} 有 {meta.slide_count} 页，"
            f"Graph 转出的 PDF 只有 {pages} 页"
        )
    logger.info("graph output verified dest=%s pages=%d", dest.name, pages)


class GraphEngine(ConversionEngine):
    """Microsoft Graph 转换引擎。

    流程：拿 token -> 上传分片到 SharePoint 中转库 -> ?format=pdf 转换
    -> 下载 -> 校验 -> 清理中转文件（成功/失败路径都清理）。

    每次转换新建实例（get_engine 的惰性构造），token 缓存只在单次转换内
    有效。切片场景下每个分片是独立 RQ job、跑在 fork 的子进程里，12 片
    就是 12 次 token 请求——token 端点响应在百毫秒级，相比每片几十秒的
    转换可以忽略，而共享 token 要引入 Redis 级缓存，等于为省几次快请求
    新增一个密钥存储面。不做。

    本类里的方法都把 client 当参数接收，所以除了"Graph 真实怎么响应"这类
    只能用真实租户验证的行为之外，控制流本身（请求次数、重试/退避、
    预算耗尽提前放弃、清理路径）都能用不到 30 行的假 client 验证，见
    tests/test_graph_engine.py。四期拿到真实租户凭证后的验证计划见
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

        try:
            resp = client.post(
                f"{LOGIN_HOST}/{tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
        except httpx.HTTPError as exc:
            # I5：DNS 失败/代理断连/网络抖动都是高频事件，裸 httpx 异常
            # 不是 AppError，会被 pipeline 的兜底归一成 INTERNAL_ERROR，
            # 还可能把异常消息写进用户可见的 error_message。
            raise _wrap_transport_errors(exc, EngineUnavailable, "Azure 认证失败") from exc
        _raise_for_status(resp, frozenset({200}), EngineUnavailable, "Azure 认证失败")
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    # ---- 重试 ----

    def _request_with_retry(
        self, client: httpx.Client, method: str, url: str, *, deadline: float, **kwargs: Any
    ) -> httpx.Response:
        """429 与 5xx 退避重试；4xx（除 429）立即失败——那是请求本身的
        问题，重试无意义。

        deadline 是 convert() 入口记下的总墙钟预算（time.monotonic() 为
        基准）。两处提前放弃都映射成 ConversionTimeout（这是"这次转换
        超时了"，不是"服务不可用"）：
        1. 发下一个请求之前预算已经耗尽——不发这个必然浪费的请求。
        2. 拿到可重试的响应之后，如果这次建议的等待时长已经不小于剩余
           预算（`wait >= remaining`），不睡——先把剩余预算睡光、醒来
           发现预算是 0 再放弃是纯浪费（睡多久结果都一样是放弃），
           SharePoint 限流常见 Retry-After 60~300 秒，切片场景下一个
           worker 为这一觉空耗的时间是可观的运力损失。这里不做"睡一个
           缩短后的时长再重试"这种折中——要么这次建议的等待完整放得进
           剩余预算就完整睡，要么放不进就立刻放弃，不折中着睡一半。
        最后一次尝试之后不 sleep（sleep 完立刻抛纯属浪费）；`graph_max_
        retries` 次全部重试用尽、但预算仍充裕时，是"服务暂时不可用"，
        落 EngineUnavailable（503）而不是 ConversionFailed——后者会让
        用户以为换个文件/重传能解决，而限流期间重传必然再失败。
        """
        last: httpx.Response | None = None
        for attempt in range(settings.graph_max_retries):
            if deadline - time.monotonic() <= 0:
                raise ConversionTimeout("Graph 转换超时：重试预算已耗尽，放弃重试")

            try:
                resp = client.request(method, url, **kwargs)
            except httpx.TimeoutException:
                raise
            except httpx.HTTPError as exc:
                raise _wrap_transport_errors(
                    exc, EngineUnavailable, "Graph 请求失败"
                ) from exc

            wait = _retry_wait_seconds(resp.status_code, resp.headers, attempt)
            if wait is None:
                return resp
            last = resp

            if attempt == settings.graph_max_retries - 1:
                break  # 最后一次不必再等——直接进入下面的耗尽处理

            remaining = deadline - time.monotonic()
            if wait >= remaining:
                # 预算不够容纳这一次建议的等待时，不能先把剩余预算睡光
                # 再放弃——那一觉必然无用（睡完预算肯定是 0，接下来必然
                # 放弃）。切片场景下一个 worker 为此空耗几百秒是可观的
                # 运力损失，直接放弃比"睡完再放弃"更省。remaining<=0 的
                # 情况被这个条件自然涵盖（wait 恒为正数，必然 >= 一个
                # <=0 的 remaining）。
                raise ConversionTimeout("Graph 转换超时：重试预算已耗尽，放弃重试")

            logger.warning(
                "Graph %d，%.0f 秒后重试（第 %d 次）", resp.status_code, wait, attempt + 1
            )
            time.sleep(wait)

        raise EngineUnavailable(
            f"Graph 限流或暂时不可用，重试 {settings.graph_max_retries} 次后仍失败："
            f"HTTP {last.status_code if last else '?'}"
            f"：{last.text[:200] if last else ''}"
        )

    # ---- 上传 ----

    def _create_upload_session(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        site_id: str,
        drive_path: str,
        src: Path,
        deadline: float,
    ) -> str:
        """建 upload session，返回 uploadUrl（供后续分片 PUT 和失败时
        取消用）。中转文件名用 _staging_filename 加唯一前缀，见其说明。"""
        resp = self._request_with_retry(
            client,
            "POST",
            _upload_session_url(site_id, drive_path, _staging_filename(src)),
            deadline=deadline,
            headers=headers,
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        _raise_for_status(resp, frozenset({200, 201}), ConversionFailed, "创建上传会话失败")
        return resp.json()["uploadUrl"]

    def _upload_chunks(
        self, client: httpx.Client, upload_url: str, src: Path, deadline: float
    ) -> str:
        """把 src 分片 PUT 到 upload_url，返回 driveItem id。

        每个分片走 _request_with_retry（I7）：Graph 官方 Best practices
        第一条就是"Resume or retry uploads that fail due to connection
        interruptions or any 5xx errors"。范围裁决：只做整块重传，不做
        `nextExpectedRanges` 那套断点续传——没有凭证的情况下没法验证，
        盲写不如不写，留到四期真机时再决定。

        upload_url 是 createUploadSession 返回的预授权 URL，不需要
        （也不应该）带 Authorization 头。
        """
        size = src.stat().st_size
        if size == 0:
            raise ConversionFailed(f"源文件 {src.name} 是 0 字节，无法上传转换")

        with src.open("rb") as fh:
            offset = 0
            while offset < size:
                block = fh.read(UPLOAD_CHUNK)
                end = offset + len(block) - 1
                put = self._request_with_retry(
                    client,
                    "PUT",
                    upload_url,
                    deadline=deadline,
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

    def _cancel_upload_session(self, client: httpx.Client, upload_url: str) -> None:
        """上传中途失败时取消会话。DELETE {uploadUrl} 是 Graph 文档给的
        取消手段："This cleans up the temporary file holding the data
        previously uploaded"（同时也提醒 "Temporary files might not be
        deleted immediately after the expiration time has elapsed"，
        所以这只是尽力而为，不是保证）。

        和 _cleanup 一样：清理失败不能掩盖原始的转换失败原因（合并模块
        踩过这个坑），只记日志，不重新抛出、不把 str(exc) 塞进日志之外的
        任何用户可见位置。
        """
        try:
            resp = client.delete(upload_url)
            if resp.status_code not in (200, 204, 404):
                logger.error("取消上传会话失败 HTTP %d", resp.status_code)
        except httpx.HTTPError as exc:
            logger.error("取消上传会话异常: %s", type(exc).__name__)

    # ---- 清理（上传成功、后续步骤失败/成功都要跑这条） ----

    def _cleanup(self, client: httpx.Client, headers: dict[str, str], site_id: str, item_id: str) -> None:
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
            logger.error("中转文件清理异常 item=%s: %s", item_id, type(exc).__name__)

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
        deadline = started + timeout_s
        upload_url: str | None = None
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
                try:
                    upload_url = self._create_upload_session(
                        client, headers, creds.site_id, creds.drive_path, src, deadline
                    )
                    item_id = self._upload_chunks(client, upload_url, src, deadline)
                except httpx.TimeoutException as exc:
                    raise ConversionTimeout(
                        f"Graph 上传超时（{settings.graph_request_timeout_s} 秒）。"
                        f"该文件可能过大或网络过慢"
                    ) from exc

                try:
                    # follow_redirects=True：?format=pdf 返回 302，Location 是
                    # 几分钟内有效的预授权 URL。不跟随就只能拿到空响应体。
                    resp = self._request_with_retry(
                        client,
                        "GET",
                        _content_url(creds.site_id, item_id),
                        deadline=deadline,
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
                # C2：item_id 只在整段上传真正完成后才赋值。如果上传中途
                # 失败（会话建好了、分块传了一部分），SharePoint 侧已经
                # 存着用户课件的字节，必须用 upload_url 取消会话，不能让
                # 它们留在共享站点里。两种清理手段互斥：item_id 存在就说明
                # 上传已完成，走正常的 _cleanup；否则如果建过会话，走取消。
                if item_id:
                    self._cleanup(client, headers, creds.site_id, item_id)
                elif upload_url:
                    self._cancel_upload_session(client, upload_url)

        _verify_pdf_output(dest, meta, src)

        logger.info(
            "graph convert done src=%s elapsed=%.1fs output=%.1fMB",
            src.name, time.monotonic() - started,
            dest.stat().st_size / 1024 / 1024,
        )
