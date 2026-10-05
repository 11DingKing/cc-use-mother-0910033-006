"""领域错误及其 HTTP 状态映射。"""
from __future__ import annotations


class DomainError(Exception):
    """领域错误基类，携带稳定的错误码与 HTTP 状态。"""

    http_status = 400
    code = "DOMAIN_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(DomainError):
    http_status = 404
    code = "NOT_FOUND"


class InvalidInput(DomainError):
    http_status = 400
    code = "INVALID_INPUT"


class InvalidState(DomainError):
    http_status = 409
    code = "INVALID_STATE"


class VersionMismatch(DomainError):
    """签署版本与当前条款版本不一致。"""

    http_status = 409
    code = "VERSION_MISMATCH"


class NotAParty(DomainError):
    http_status = 403
    code = "NOT_A_PARTY"


class QuoteExpired(DomainError):
    http_status = 409
    code = "QUOTE_EXPIRED"


class InsufficientQuota(DomainError):
    http_status = 422
    code = "INSUFFICIENT_QUOTA"


class CallbackConflict(DomainError):
    """清算回调与已记录交割进度冲突。"""

    http_status = 409
    code = "CALLBACK_CONFLICT"


class UnbalancedEntry(DomainError):
    http_status = 500
    code = "UNBALANCED_ENTRY"
