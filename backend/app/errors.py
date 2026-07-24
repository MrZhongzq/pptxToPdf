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
