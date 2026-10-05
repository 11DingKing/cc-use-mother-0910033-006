"""复式台账：所有额度变动以平衡分录落账，账户余额由分录推导。"""
from __future__ import annotations

from .errors import InsufficientQuota, UnbalancedEntry
from .models import Asset, Direction, LedgerEntry

SYSTEM_PREFIX = "SYSTEM:"

_BUCKETS = ("available", "frozen", "in_transit")


def account_of(party: str, asset: Asset, bucket: str) -> str:
    """账户命名：{主体}:{资产}:{available|frozen|in_transit}。"""
    if bucket not in _BUCKETS:
        raise ValueError(f"未知账户桶：{bucket}")
    return f"{party}:{asset.value}:{bucket}"


def system_account(asset: Asset) -> str:
    return f"{SYSTEM_PREFIX}{asset.value}:available"


class Ledger:
    """只增不改的分录日志；余额随分录落账同步更新。"""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._balances: dict[str, int] = {}

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def balance(self, account: str) -> int:
        return self._balances.get(account, 0)

    def post(self, entry: LedgerEntry) -> None:
        """校验并落账：分录按资产平衡，且不得使普通账户透支。"""
        deltas: dict[str, int] = {}
        per_asset: dict[Asset, int] = {}
        for line in entry.lines:
            if line.amount <= 0:
                raise UnbalancedEntry("分录行金额必须为正整数")
            signed = line.amount if line.direction is Direction.DEBIT else -line.amount
            deltas[line.account] = deltas.get(line.account, 0) + signed
            per_asset[line.asset] = per_asset.get(line.asset, 0) + signed
        unbalanced = sorted(asset.value for asset, total in per_asset.items() if total != 0)
        if unbalanced:
            raise UnbalancedEntry("分录未按资产平衡：" + "、".join(unbalanced))
        for account, delta in deltas.items():
            if not account.startswith(SYSTEM_PREFIX) and self.balance(account) + delta < 0:
                raise InsufficientQuota(f"账户 {account} 余额不足，无法落账 {entry.entry_id}")
        self._entries.append(entry)
        for account, delta in deltas.items():
            self._balances[account] = self.balance(account) + delta

    def entries_for_quote(self, quote_id: str) -> list[LedgerEntry]:
        return [entry for entry in self._entries if entry.quote_id == quote_id]

    def entries_for_party(self, party: str) -> list[LedgerEntry]:
        prefix = f"{party}:"
        return [
            entry
            for entry in self._entries
            if any(line.account.startswith(prefix) for line in entry.lines)
        ]
