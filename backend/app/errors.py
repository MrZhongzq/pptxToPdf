class AppError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message: str = "") -> None:
        self.message = message or self.code
        super().__init__(self.message)


class UploadSessionNotFound(AppError):
    code = "UPLOAD_SESSION_NOT_FOUND"
    http_status = 404


class UploadSessionExpired(AppError):
    code = "UPLOAD_SESSION_EXPIRED"
    http_status = 410


class UploadSessionNotActive(AppError):
    """会话存在且未过期，但状态已不是 active（如已 completed）——
    典型触发场景：complete 之后迟到的分片 PUT（网络层重传、abort 后仍在飞的请求）。"""

    code = "UPLOAD_SESSION_NOT_ACTIVE"
    http_status = 409


class ValidationError(AppError):
    """FastAPI/pydantic 请求体校验失败时的统一错误码，供 RequestValidationError
    处理器复用，保持所有错误响应的码值都来自本模块这一个来源。"""

    code = "VALIDATION_ERROR"
    http_status = 422


class UploadSizeExceeded(AppError):
    code = "UPLOAD_SIZE_EXCEEDED"
    http_status = 413


class UploadIncomplete(AppError):
    code = "UPLOAD_INCOMPLETE"
    http_status = 409


class UploadChecksumMismatch(AppError):
    code = "UPLOAD_CHECKSUM_MISMATCH"
    http_status = 422


class PptxInvalidZip(AppError):
    code = "PPTX_INVALID_ZIP"
    http_status = 422


class PptxNotPresentation(AppError):
    code = "PPTX_NOT_PRESENTATION"
    http_status = 422


class PptxEncrypted(AppError):
    code = "PPTX_ENCRYPTED"
    http_status = 422


class StorageFull(AppError):
    code = "STORAGE_FULL"
    http_status = 507


class ConversionFailed(AppError):
    code = "CONVERSION_FAILED"
    http_status = 500


class ConversionTimeout(AppError):
    code = "CONVERSION_TIMEOUT"
    http_status = 504


class ConversionPageMismatch(AppError):
    code = "CONVERSION_PAGE_MISMATCH"
    http_status = 500


class EngineUnavailable(AppError):
    code = "ENGINE_UNAVAILABLE"
    http_status = 503


class ResultExpired(AppError):
    code = "RESULT_EXPIRED"
    http_status = 410


class TaskAbandoned(AppError):
    code = "TASK_ABANDONED"
    http_status = 410


class ShardTooLarge(AppError):
    """单个分片实测体积超上限、且已经只剩 1 页——页面不可再分，切不动了。

    与 ShardBudgetExceeded 分开：这一条说的是"这份文件里有一页大到无解"
    （用户可以拆掉那页的内嵌视频重试），另一条说的是"整份文件的分片总量
    超出了我们的合并预算"（用户只能拆分文件）。给出的处置建议不同，
    所以不共用一个码。
    """

    code = "SHARD_TOO_LARGE"
    http_status = 422


class ShardBudgetExceeded(AppError):
    """分片总数或合并输入总字节超过显式上限。

    merge_pdfs 把所有分片一次性载入同一个 PdfWriter（pypdf 没有真正的流式
    合并 API），实测峰值约 2.2 倍输入体积。没有这道闸门，<2GB 内存的机器上
    worker 会被 OOM killer 干掉——那是最坏的静默失败：任务卡在 converting
    永不回收，前端一直轮询一个永不改变的状态。
    """

    code = "SHARD_BUDGET_EXCEEDED"
    http_status = 422


class GraphNotConfigured(AppError):
    code = "GRAPH_NOT_CONFIGURED"
    http_status = 503


class AdminNotConfigured(AppError):
    """未设置 PPTX2PDF_ADMIN_PASSWORD_HASH。整个管理入口不可用——
    不提供「没设密码就免密进入」的默认行为，与 GraphNotConfigured 同构。"""

    code = "ADMIN_NOT_CONFIGURED"
    http_status = 503


class AdminUnauthorized(AppError):
    code = "ADMIN_UNAUTHORIZED"
    http_status = 401


class AdminBadPassword(AppError):
    code = "ADMIN_BAD_PASSWORD"
    http_status = 401


class GraphSelftestFailed(AppError):
    """五步连通性自检未全绿。响应体里带每步状态，调用方据此定位。"""

    code = "GRAPH_SELFTEST_FAILED"
    http_status = 422

    def __init__(self, message: str = "", *, steps: list | None = None) -> None:
        super().__init__(message or "Graph 凭证自检未通过")
        self.steps = steps or []


class ReadyExpired(AppError):
    """上传后超时未点「开始转换」，原文件已被回收。

    与 TASK_ABANDONED 是两个不同的失败原因，不要混用：那个是「转换
    过程中卡死了」，这个是「你没点开始」。
    """

    code = "READY_EXPIRED"
    http_status = 410


class AuthRequired(AppError):
    """需要登录。与 ADMIN_UNAUTHORIZED 分开：那个是「管理入口的会话无效」，
    这个是「这个动作需要一个已登录用户」，前端对两者的反应不同——前者跳
    管理登录页，后者弹右上角的登录框。"""

    code = "AUTH_REQUIRED"
    http_status = 401


class Forbidden(AppError):
    """已登录但权限不够。不与 AUTH_REQUIRED 合并：401 让前端弹登录框，
    而这里再弹一次登录框只会让人反复输入正确的密码却仍被拒。"""

    code = "FORBIDDEN"
    http_status = 403


class UserNotFound(AppError):
    code = "USER_NOT_FOUND"
    http_status = 404


class UsernameTaken(AppError):
    code = "USERNAME_TAKEN"
    http_status = 409


class BadCredentials(AppError):
    """用户名或密码错误。刻意不区分是哪一个——区分等于把用户名枚举
    送给攻击者。"""

    code = "BAD_CREDENTIALS"
    http_status = 401


class CrossOriginBlocked(AppError):
    """来源不在白名单。六期先建骨架，默认不启用。"""

    code = "CROSS_ORIGIN_BLOCKED"
    http_status = 403


class OriginBlocked(AppError):
    """来源在黑名单里。网页与 v1 一起拦，优先级高于白名单。

    与 CROSS_ORIGIN_BLOCKED 分开：那个是「你不在 v1 的白名单里」，
    只影响 v1；这个是「你被整站拒绝」。两者对调用方的含义不同，
    合并成一个码会让排查时分不清是哪道门把人挡在外面。
    """

    code = "ORIGIN_BLOCKED"
    http_status = 403
