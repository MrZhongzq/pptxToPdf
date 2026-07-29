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
