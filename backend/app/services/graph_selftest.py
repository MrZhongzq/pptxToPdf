"""Graph 凭证的五步连通性自检。

不复用 GraphEngine：那边刻意把错误归一成 ConversionFailed /
EngineUnavailable / ConversionTimeout 以便流水线统一处理，而这里
需要的恰好相反——尽可能区分错误，还要知道卡在第几步、Graph 返回的
原始 error code 是什么。复用只有两条路：改造 GraphEngine 让它吐更多
细节（污染一个已为转换优化好的接口），或在这里解析归一后的中文消息
字符串（脆弱）。代价是一部分 HTTP 调用逻辑重复，判断依据是诊断价值
高于这部分重复的维护成本。
"""

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.engines.graph import GRAPH_ROOT, LOGIN_HOST
from app.services.graph_credentials import GraphCredentialData

logger = logging.getLogger(__name__)

STEPS = ("token", "drive", "upload", "convert", "delete")

_BODY_LIMIT = 300


@dataclass
class StepResult:
    step: str
    ok: bool | None
    """None 表示因前序步骤失败而未执行，与 False（执行了但失败）区分。"""
    detail: str | None


def _clip(body: str) -> str:
    return body if len(body) <= _BODY_LIMIT else body[:_BODY_LIMIT] + "…"


def diagnose_token_error(status: int, body: str) -> str:
    """AADSTS 错误码能区分租户、client_id、client_secret 三类错误。"""
    if "AADSTS90002" in body:
        return f"租户不存在或 tenant_id 写错（AADSTS90002）。原始响应：{_clip(body)}"
    if "AADSTS700016" in body:
        return f"该租户下找不到这个应用，client_id 写错或应用未创建（AADSTS700016）。原始响应：{_clip(body)}"
    if "AADSTS7000215" in body:
        return f"client_secret 错误或已过期（AADSTS7000215）。原始响应：{_clip(body)}"
    return f"取 access token 失败（HTTP {status}）。原始响应：{_clip(body)}"


def diagnose_graph_error(step: str, status: int, body: str) -> str:
    if step == "drive":
        if status == 404:
            return f"找不到该站点或站点下的文档库，site_id 可能写错（HTTP 404）。原始响应：{_clip(body)}"
        if status == 403:
            return f"应用对该站点没有权限，检查 API 权限是否已授予且管理员同意已点（HTTP 403）。原始响应：{_clip(body)}"
    if step == "upload" and status == 403:
        return f"没有写权限，无法上传到中转库（HTTP 403）。原始响应：{_clip(body)}"
    if step == "delete" and status == 403:
        return (
            "没有永久删除权限（HTTP 403）。permanentDelete 需要 "
            "Files.ReadWrite.All 或 Sites.ReadWrite.All，Sites.Selected 不够。"
            f"原始响应：{_clip(body)}"
        )
    return f"{step} 步失败（HTTP {status}）。原始响应：{_clip(body)}"


# ---- 五步连通性自检编排 ----

SELFTEST_PPTX = Path(__file__).resolve().parent.parent / "assets" / "selftest.pptx"

# 固定前缀：进程若在上传与删除之间被杀，能人工识别并清理租户里的残留。
_STAGING_PREFIX = "pptx2pdf-selftest-"


def _drive_url(site_id: str) -> str:
    return f"{GRAPH_ROOT}/sites/{quote(site_id, safe='')}/drive"


def _upload_session_url(site_id: str, drive_path: str, filename: str) -> str:
    site = quote(site_id, safe="")
    target = f"{drive_path}/{filename}"
    return f"{GRAPH_ROOT}/sites/{site}/drive/root:/{quote(target, safe='/')}:/createUploadSession"


def _content_url(site_id: str, item_id: str) -> str:
    site = quote(site_id, safe="")
    return f"{GRAPH_ROOT}/sites/{site}/drive/items/{quote(item_id, safe='')}/content?format=pdf"


def _delete_url(site_id: str, item_id: str) -> str:
    site = quote(site_id, safe="")
    return f"{GRAPH_ROOT}/sites/{site}/drive/items/{quote(item_id, safe='')}/permanentDelete"


def run_selftest(
    creds: GraphCredentialData, *, client_factory=httpx.Client
) -> list[StepResult]:
    """跑完五步，返回每步结果。任何一步失败则后续步骤 ok=None。

    永远返回列表、不抛异常——调用方要的是诊断清单而不是一个异常。
    """
    results = {step: StepResult(step=step, ok=None, detail=None) for step in STEPS}
    timeout = settings.graph_request_timeout_s

    with client_factory(timeout=timeout, follow_redirects=True) as client:
        token = _step_token(client, creds, results)
        if token is None:
            return [results[s] for s in STEPS]

        headers = {"Authorization": f"Bearer {token}"}
        if not _step_drive(client, creds, headers, results):
            return [results[s] for s in STEPS]

        item_id = _step_upload(client, creds, headers, results)
        if item_id is None:
            return [results[s] for s in STEPS]

        _step_convert(client, creds, headers, item_id, results)
        _step_delete(client, creds, headers, item_id, results)

    return [results[s] for s in STEPS]


def _step_token(client, creds, results) -> str | None:
    url = f"{LOGIN_HOST}/{quote(creds.tenant_id, safe='')}/oauth2/v2.0/token"
    try:
        resp = client.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
    except httpx.HTTPError as exc:
        results["token"] = StepResult("token", False, f"网络错误：{type(exc).__name__}")
        return None
    if resp.status_code != 200:
        results["token"] = StepResult("token", False, diagnose_token_error(resp.status_code, resp.text))
        return None
    token = resp.json().get("access_token")
    if not token:
        results["token"] = StepResult("token", False, "响应里没有 access_token")
        return None
    results["token"] = StepResult("token", True, None)
    return token


def _step_drive(client, creds, headers, results) -> bool:
    try:
        resp = client.get(_drive_url(creds.site_id), headers=headers)
    except httpx.HTTPError as exc:
        results["drive"] = StepResult("drive", False, f"网络错误：{type(exc).__name__}")
        return False
    if resp.status_code != 200:
        results["drive"] = StepResult("drive", False, diagnose_graph_error("drive", resp.status_code, resp.text))
        return False
    results["drive"] = StepResult("drive", True, None)
    return True


def _step_upload(client, creds, headers, results) -> str | None:
    filename = f"{_STAGING_PREFIX}{uuid.uuid4().hex}.pptx"
    payload = SELFTEST_PPTX.read_bytes()
    try:
        session_resp = client.post(
            _upload_session_url(creds.site_id, creds.drive_path, filename),
            headers=headers,
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        if session_resp.status_code not in (200, 201):
            results["upload"] = StepResult(
                "upload", False, diagnose_graph_error("upload", session_resp.status_code, session_resp.text)
            )
            return None
        upload_url = session_resp.json()["uploadUrl"]

        total = len(payload)
        put_resp = client.put(
            upload_url,
            headers={
                "Content-Length": str(total),
                "Content-Range": f"bytes 0-{total - 1}/{total}",
            },
            content=payload,
        )
    except httpx.HTTPError as exc:
        results["upload"] = StepResult("upload", False, f"网络错误：{type(exc).__name__}")
        return None
    if put_resp.status_code not in (200, 201):
        results["upload"] = StepResult(
            "upload", False, diagnose_graph_error("upload", put_resp.status_code, put_resp.text)
        )
        return None
    item_id = put_resp.json().get("id")
    if not item_id:
        results["upload"] = StepResult("upload", False, "上传完成但响应里没有 driveItem id")
        return None
    results["upload"] = StepResult("upload", True, None)
    return item_id


def _step_convert(client, creds, headers, item_id, results) -> None:
    try:
        resp = client.get(_content_url(creds.site_id, item_id), headers=headers)
    except httpx.HTTPError as exc:
        results["convert"] = StepResult("convert", False, f"网络错误：{type(exc).__name__}")
        return
    if resp.status_code != 200:
        results["convert"] = StepResult(
            "convert", False, diagnose_graph_error("convert", resp.status_code, resp.text)
        )
        return
    if not resp.content.startswith(b"%PDF"):
        results["convert"] = StepResult(
            "convert", False, "转换返回的内容不是 PDF——可能被重定向到了登录页"
        )
        return
    results["convert"] = StepResult("convert", True, None)


def _step_delete(client, creds, headers, item_id, results) -> None:
    """无论转换成功与否都要跑——失败路径上的中转文件同样必须删掉。"""
    try:
        resp = client.post(_delete_url(creds.site_id, item_id), headers=headers)
    except httpx.HTTPError as exc:
        results["delete"] = StepResult("delete", False, f"网络错误：{type(exc).__name__}")
        return
    if resp.status_code not in (200, 204):
        results["delete"] = StepResult(
            "delete", False, diagnose_graph_error("delete", resp.status_code, resp.text)
        )
        return
    results["delete"] = StepResult("delete", True, None)
