"""双边成交确认后端的领域异常。"""
from __future__ import annotations


class DomainError(Exception):
    """领域规则违例的基类。"""


class NotFoundError(DomainError):
    """目标资源不存在。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


class InsufficientQuotaError(DomainError):
    """可用额度不足，无法冻结。"""
