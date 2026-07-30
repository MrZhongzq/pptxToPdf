"""按 URL 下载文件，带 SSRF 防护。

v1 接口的 `fileUrl` 由调用方给，服务器会去请求它。不设防的话，任何人
都能让这台机器去访问它自己的内网——包括云厂商的元数据端点
（169.254.169.254，能拿到实例凭证）、内网管理面板、数据库端口。

这里的防护**不做成开关**：一个可以关掉的 SSRF 防护，迟早会因为「某次
调试方便」被关掉再也没打开。
"""

import ipaddress
import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.errors import AppError

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 5


class UrlNotAllowed(AppError):
    """目标地址不允许访问（协议不对、指向内网、解析失败）。"""

    code = "URL_NOT_ALLOWED"
    http_status = 400


class DownloadFailed(AppError):
    code = "DOWNLOAD_FAILED"
    http_status = 502


class DownloadTooLarge(AppError):
    code = "DOWNLOAD_TOO_LARGE"
    http_status = 413


@dataclass
class FetchResult:
    path: Path
    size_bytes: int
    filename: str


def _resolved_addresses(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UrlNotAllowed(f"无法解析主机名 {hostname}") from exc
    return [info[4][0] for info in infos]


def assert_public_url(url: str) -> None:
    """校验 URL 可以被访问。不通过就抛，绝不静默改写或降级。

    **每一跳重定向都要重新调用这个函数**：只查首个 URL 等于没查——
    「返回 302 指向 169.254.169.254」是最常见的绕过手法。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UrlNotAllowed(f"只允许 http/https，收到 {parsed.scheme or '(空)'}")
    hostname = parsed.hostname
    if not hostname:
        raise UrlNotAllowed("URL 缺少主机名")

    for addr in _resolved_addresses(hostname):
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise UrlNotAllowed(f"无法解析地址 {addr}") from None
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            # 不回显具体 IP：那等于把内网拓扑当成探测接口送出去
            raise UrlNotAllowed(f"{hostname} 指向内网或保留地址，拒绝访问")


def _filename_from(url: str, headers) -> str:
    disposition = headers.get("content-disposition", "")
    if "filename=" in disposition:
        raw = disposition.split("filename=", 1)[1].strip().strip('"; ')
        if raw:
            return raw
    name = Path(urlparse(url).path).name
    return name or "download.pptx"


def fetch_to(url: str, dest: Path, *, timeout_s: float = 60.0) -> FetchResult:
    """下载到 dest。边下边计数，超过 max_file_size 立刻断开。

    手工跟随重定向（`follow_redirects=False`）而不是交给 httpx：只有自己
    跟，才能在**每一跳**上重新跑 assert_public_url。
    """
    current = url
    assert_public_url(current)

    written = 0
    limit = settings.max_file_size

    with httpx.Client(follow_redirects=False, timeout=timeout_s) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise DownloadFailed("重定向响应缺少 Location")
                        current = str(resp.url.join(location))
                        assert_public_url(current)
                        continue

                    if resp.status_code >= 400:
                        raise DownloadFailed(f"下载失败，源站返回 HTTP {resp.status_code}")

                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise DownloadTooLarge(
                            f"文件超过上限 {limit // (1024 * 1024)} MB"
                        )

                    with dest.open("wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                            written += len(chunk)
                            if written > limit:
                                # 边下边判，不能只信 Content-Length——那个头
                                # 是源站说的，可以撒谎也可以干脆不给
                                fh.close()
                                dest.unlink(missing_ok=True)
                                raise DownloadTooLarge(
                                    f"文件超过上限 {limit // (1024 * 1024)} MB"
                                )
                            fh.write(chunk)

                    return FetchResult(dest, written, _filename_from(current, resp.headers))
            except httpx.HTTPError as exc:
                dest.unlink(missing_ok=True)
                raise DownloadFailed(f"下载失败: {exc}") from exc

    dest.unlink(missing_ok=True)
    raise DownloadFailed(f"重定向次数超过 {MAX_REDIRECTS} 次")
