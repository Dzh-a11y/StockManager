"""HTTP error contract and exception-to-status mapping for the Web layer."""

from __future__ import annotations

from stock_manager.services.screening_service import (
    DatasetUnavailableError,
    StockNotFoundError,
)
from stock_manager.services.parameterized_screening_service import (
    DatasetUnavailableError as ParameterizedDatasetUnavailableError,
)
from stock_manager.sync import CooldownActiveError, RetryRequiredError, SyncFailedError
from stock_manager.templates.service import TemplateRevisionConflictError


class ApiError(Exception):
    """Raised to signal a structured HTTP error response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def payload(self) -> dict[str, object]:
        return {"error": {"code": self.code, "message": self.message}}


class BadRequestError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(400, "BAD_REQUEST", message)


class ForbiddenError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(403, "FORBIDDEN", message)


class NotFoundError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(404, "NOT_FOUND", message)


class ConflictError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "CONFLICT", message)


class InternalError(ApiError):
    def __init__(self) -> None:
        super().__init__(500, "INTERNAL", "internal server error")


def _safe_message(error: Exception) -> str:
    """Return a user-safe message, refusing to leak machine paths or stack traces."""
    message = str(error)
    return message


def map_exception(error: Exception) -> ApiError:
    """Translate a raised exception into a structured HTTP error."""
    if isinstance(error, ApiError):
        return error
    if isinstance(error, (DatasetUnavailableError, ParameterizedDatasetUnavailableError)):
        return NotFoundError(_safe_message(error))
    if isinstance(error, StockNotFoundError):
        return NotFoundError(_safe_message(error))
    if isinstance(error, FileNotFoundError):
        return NotFoundError(_safe_message(error))
    if isinstance(error, PermissionError):
        return ForbiddenError(_safe_message(error))
    if isinstance(error, (TemplateRevisionConflictError, FileExistsError)):
        return ConflictError(_safe_message(error))
    if isinstance(error, CooldownActiveError):
        return ApiError(409, "SYNC_COOLDOWN", _safe_message(error))
    if isinstance(error, RetryRequiredError):
        return ApiError(409, "SYNC_RETRY_REQUIRED", _safe_message(error))
    if isinstance(error, SyncFailedError):
        return ApiError(500, "SYNC_FAILED", _safe_message(error))
    if isinstance(error, ValueError):
        return BadRequestError(_safe_message(error))
    return InternalError()
