"""台账：账户余额与全量分录。

所有资金变动都先落分录再改余额，分录不可篡改、可回放校验；
补偿类分录（RELEASE / COMPENSATE）通过 compensates 字段引用被冲销的原分录。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .errors import DomainError, InsufficientQuotaError
from .models import Asset, EntryType


@dataclass
class Account:
    """某个主体在某类资产上的余额。"""

    owner: str
    asset: Asset
    available: int = 0
    frozen: int = 0


@dataclass
class LedgerEntry:
    """一条台账分录。available_delta / frozen_delta 记录对余额的影响。"""

    seq: int
    entry_id: str
    owner: str
    asset: Asset
    available_delta: int
    frozen_delta: int
    entry_type: EntryType
    ref: str  # 关联业务对象（报价、清算指令等）
    note: str
    created_at: float
    compensates: str | None = None  # 补偿分录引用的原分录编号

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "entry_id": self.entry_id,
            "owner": self.owner,
            "asset": self.asset.value,
            "available_delta": self.available_delta,
            "frozen_delta": self.frozen_delta,
            "entry_type": self.entry_type.value,
            "ref": self.ref,
            "note": self.note,
            "created_at": self.created_at,
            "compensates": self.compensates,
        }


class Ledger:
    """账户与分录的唯一写入点。"""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._accounts: dict[tuple[str, Asset], Account] = {}
        self.entries: list[LedgerEntry] = []

    # ---- 查询 ----

    def account(self, owner: str, asset: Asset) -> Account:
        key = (owner, asset)
        if key not in self._accounts:
            self._accounts[key] = Account(owner=owner, asset=asset)
        return self._accounts[key]

    def entries_for_ref(self, ref_prefix: str) -> list[LedgerEntry]:
        return [e for e in self.entries if e.ref.startswith(ref_prefix)]

    def entries_for_owner(self, owner: str) -> list[LedgerEntry]:
        return [e for e in self.entries if e.owner == owner]

    def verify(self) -> None:
        """回放全部分录，校验账户余额与分录累计一致且不为负。"""
        replay: dict[tuple[str, Asset], list[int]] = {}
        for entry in self.entries:
            key = (entry.owner, entry.asset)
            bal = replay.setdefault(key, [0, 0])
            bal[0] += entry.available_delta
            bal[1] += entry.frozen_delta
        for key, (available, frozen) in replay.items():
            account = self.account(*key)
            if (account.available, account.frozen) != (available, frozen):
                raise DomainError(f"台账不一致：{key} 分录回放 {available}/{frozen}，实际 {account.available}/{account.frozen}")
        for account in self._accounts.values():
            if account.available < 0 or account.frozen < 0:
                raise DomainError(f"台账出现负余额：{account.owner} {account.asset.value}")

    # ---- 写入 ----

    def _post(
        self,
        owner: str,
        asset: Asset,
        available_delta: int,
        frozen_delta: int,
        entry_type: EntryType,
        ref: str,
        note: str,
        compensates: str | None = None,
    ) -> LedgerEntry:
        account = self.account(owner, asset)
        account.available += available_delta
        account.frozen += frozen_delta
        if account.available < 0 or account.frozen < 0:  # 安全网：正常流程在校验阶段就会拒绝
            raise AssertionError(f"分录导致负余额：{owner} {asset.value}")
        entry = LedgerEntry(
            seq=len(self.entries) + 1,
            entry_id=f"E{len(self.entries) + 1:06d}",
            owner=owner,
            asset=asset,
            available_delta=available_delta,
            frozen_delta=frozen_delta,
            entry_type=entry_type,
            ref=ref,
            note=note,
            created_at=self._clock(),
            compensates=compensates,
        )
        self.entries.append(entry)
        return entry

    def deposit(self, owner: str, asset: Asset, amount: int, note: str = "注资") -> LedgerEntry:
        """初始化注资（测试与运营入账入口）。"""
        if amount <= 0:
            raise DomainError("注资金额必须为正")
        return self._post(owner, asset, amount, 0, EntryType.DEPOSIT, f"deposit:{owner}", note)

    def ensure_sufficient(self, owner: str, asset: Asset, amount: int, extra_available: int = 0) -> None:
        """校验可用额度（可计入即将释放的额度），只校验不改动。"""
        if self.account(owner, asset).available + extra_available < amount:
            raise InsufficientQuotaError(f"{owner} 的 {asset.value} 可用额度不足：需要 {amount}")

    def freeze(self, owner: str, asset: Asset, amount: int, ref: str, note: str) -> LedgerEntry:
        """冻结：可用转冻结。"""
        self.ensure_sufficient(owner, asset, amount)
        return self._post(owner, asset, -amount, amount, EntryType.FREEZE, ref, note)

    def release(self, owner: str, asset: Asset, amount: int, ref: str, note: str, compensates: str) -> LedgerEntry:
        """释放冻结（补偿分录），必须引用原 FREEZE 分录。"""
        return self._post(owner, asset, amount, -amount, EntryType.RELEASE, ref, note, compensates=compensates)

    def consume_frozen(self, owner: str, asset: Asset, amount: int, ref: str, note: str) -> LedgerEntry:
        """成交时消耗冻结额度，转入清算托管。"""
        return self._post(owner, asset, 0, -amount, EntryType.SETTLE, ref, note)

    def credit(self, owner: str, asset: Asset, amount: int, ref: str, note: str) -> LedgerEntry:
        """清算划转入账。"""
        return self._post(owner, asset, amount, 0, EntryType.SETTLE, ref, note)

    def debit_available(self, owner: str, asset: Asset, amount: int, ref: str, note: str) -> LedgerEntry:
        """清算划转出账。"""
        return self._post(owner, asset, -amount, 0, EntryType.SETTLE, ref, note)

    def compensate(self, owner: str, asset: Asset, amount: int, ref: str, note: str, compensates: str) -> LedgerEntry:
        """补偿分录：把未交割余量退回原主，必须引用原 SETTLE 分录。"""
        return self._post(owner, asset, amount, 0, EntryType.COMPENSATE, ref, note, compensates=compensates)
