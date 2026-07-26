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
    code = "SHARD_TOO_LARGE"
    http_status = 422


class GraphNotConfigured(AppError):
    code = "GRAPH_NOT_CONFIGURED"
    http_status = 503
