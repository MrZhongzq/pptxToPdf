"""Graph 凭证的五步连通性自检。

不复用 GraphEngine：那边刻意把错误归一成 ConversionFailed /
EngineUnavailable / ConversionTimeout 以便流水线统一处理，而这里
需要的恰好相反——尽可能区分错误，还要知道卡在第几步、Graph 返回的
原始 error code 是什么。复用只有两条路：改造 GraphEngine 让它吐更多
细节（污染一个已为转换优化好的接口），或在这里解析归一后的中文消息
字符串（脆弱）。代价是一部分 HTTP 调用逻辑重复，判断依据是诊断价值
高于这部分重复的维护成本。
"""

from dataclasses import dataclass

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
