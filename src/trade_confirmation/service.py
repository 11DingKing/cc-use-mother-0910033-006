"""双边成交确认的核心服务。

关键规则：
- 只有同一版本集齐买卖双方签署才原子成交；跨版本签署不可能成交，
  因为修订会使旧版本签署失效并释放其冻结；
- 版本生效即双向冻结（卖方积分、买方资金），先校验后冻结，保证原子性；
- 单方撤回、确认超时、部分交割余量，一律通过补偿分录冲回，且在同一
  锁内完成，杜绝"只释放一方额度"；
- 交割回调按 callback_id 幂等，重复回调直接返回首次处理结果。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .errors import DomainError, NotFoundError, StateError
from .ledger import Ledger
from .models import (
    Asset,
    ClearingInstruction,
    ClearingStatus,
    EntryType,
    Freeze,
    FreezeStatus,
    Party,
    Quote,
    QuoteStatus,
    QuoteVersion,
    Signature,
    SignatureStatus,
    Terms,
    VersionStatus,
)


class TradeConfirmationService:
    """报价版本、双方签署、额度冻结、确认期限与清算指令的统一入口。"""

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.time
        self.ledger = Ledger(self._clock)
        self._quotes: dict[str, Quote] = {}
        self._clearing: dict[str, ClearingInstruction] = {}
        self._freezes: dict[str, Freeze] = {}
        self._client_refs: dict[str, str] = {}  # client_ref -> quote_id（创建幂等）
        self._timelines: dict[str, list[dict]] = {}
        self._event_seq = 0
        self._quote_seq = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 账户
    # ------------------------------------------------------------------

    def deposit(self, owner: str, asset: Asset | str, amount: int) -> dict:
        """注资入口（初始化/测试用）。"""
        with self._lock:
            entry = self.ledger.deposit(owner, Asset(asset), int(amount))
            return entry.to_dict()

    def get_account(self, owner: str) -> dict:
        """查看某主体的余额与全部分录。"""
        with self._lock:
            balances = {
                asset.value: {
                    "available": self.ledger.account(owner, asset).available,
                    "frozen": self.ledger.account(owner, asset).frozen,
                }
                for asset in Asset
            }
            return {
                "owner": owner,
                "balances": balances,
                "entries": [e.to_dict() for e in self.ledger.entries_for_owner(owner)],
            }

    # ------------------------------------------------------------------
    # 报价生命周期
    # ------------------------------------------------------------------

    def create_quote(
        self,
        buyer: str,
        seller: str,
        points: int,
        unit_price_cents: int,
        confirm_seconds: float,
        client_ref: str | None = None,
    ) -> dict:
        """录入报价条款，生成草稿（版本 1 未发布，尚未冻结额度）。"""
        with self._lock:
            if client_ref and client_ref in self._client_refs:
                return self.get_quote(self._client_refs[client_ref])  # 幂等
            terms = self._validate_terms(points, unit_price_cents)
            if confirm_seconds <= 0:
                raise DomainError("确认期限必须为正")
            if buyer == seller:
                raise DomainError("买卖双方不能是同一主体")
            self._quote_seq += 1
            quote_id = f"Q-{self._quote_seq:04d}"
            quote = Quote(quote_id=quote_id, buyer=buyer, seller=seller, created_at=self._clock())
            quote.versions.append(QuoteVersion(version_no=1, terms=terms, confirm_seconds=float(confirm_seconds)))
            self._quotes[quote_id] = quote
            self._timelines[quote_id] = []
            if client_ref:
                self._client_refs[client_ref] = quote_id
            self._event(quote_id, "QUOTE_CREATED", f"报价 {quote_id} 创建（草稿），买方 {buyer}，卖方 {seller}")
            return self.get_quote(quote_id)

    def publish(self, quote_id: str) -> dict:
        """发布当前版本：双向冻结额度并进入协商中。"""
        with self._lock:
            quote = self._require_quote(quote_id)
            if quote.status is not QuoteStatus.DRAFT:
                raise StateError(f"仅草稿可发布，当前状态 {quote.status.value}")
            version = quote.current_version
            # 先校验双方额度，再统一冻结，保证原子性
            self.ledger.ensure_sufficient(quote.seller, Asset.POINT, version.terms.points)
            self.ledger.ensure_sufficient(quote.buyer, Asset.CASH, version.terms.total_cents)
            now = self._clock()
            version.status = VersionStatus.ACTIVE
            version.published_at = now
            version.confirm_deadline = now + version.confirm_seconds
            quote.status = QuoteStatus.OPEN
            self._freeze_pair(quote, version)
            self._event(
                quote_id,
                "PUBLISHED",
                f"版本 v{version.version_no} 发布，确认期限 {version.confirm_seconds:.0f} 秒，双方额度已冻结",
            )
            return self.get_quote(quote_id)

    def revise(self, quote_id: str, points: int, unit_price_cents: int, confirm_seconds: float) -> dict:
        """修订条款：旧版本作废、旧签署失效并释放旧冻结，新版本立即生效。"""
        with self._lock:
            quote = self._require_quote(quote_id)
            self._expire_if_due(quote)
            if quote.status is not QuoteStatus.OPEN:
                raise StateError(f"仅协商中可修订，当前状态 {quote.status.value}")
            terms = self._validate_terms(points, unit_price_cents)
            if confirm_seconds <= 0:
                raise DomainError("确认期限必须为正")
            old = quote.current_version
            # 原子性校验：释放旧冻结后的可用额度必须覆盖新冻结
            self.ledger.ensure_sufficient(quote.seller, Asset.POINT, terms.points, extra_available=old.terms.points)
            self.ledger.ensure_sufficient(quote.buyer, Asset.CASH, terms.total_cents, extra_available=old.terms.total_cents)
            self._void_version(quote, old, VersionStatus.SUPERSEDED, reason="条款修订")
            now = self._clock()
            version = QuoteVersion(
                version_no=old.version_no + 1,
                terms=terms,
                confirm_seconds=float(confirm_seconds),
                status=VersionStatus.ACTIVE,
                published_at=now,
                confirm_deadline=now + float(confirm_seconds),
            )
            quote.versions.append(version)
            self._freeze_pair(quote, version)
            self._event(quote_id, "REVISED", f"条款修订为 v{version.version_no}，旧签署已失效并释放冻结")
            return self.get_quote(quote_id)

    def sign(self, quote_id: str, party: Party | str) -> dict:
        """一方签署当前版本；集齐同一版本双签即原子成交。"""
        with self._lock:
            quote = self._require_quote(quote_id)
            self._expire_if_due(quote)
            if quote.status is not QuoteStatus.OPEN:
                raise StateError(f"仅协商中可签署，当前状态 {quote.status.value}")
            party = self._parse_party(party)
            version = quote.current_version
            existing = version.signature_of(party)
            if existing is not None and existing.status is SignatureStatus.VALID:
                return self.get_quote(quote_id)  # 重复签署幂等
            version.signatures.append(Signature(party=party, version_no=version.version_no, signed_at=self._clock()))
            self._event(quote_id, "SIGNED", f"{party.value} 签署 v{version.version_no}")
            if version.has_valid_signature(Party.BUYER) and version.has_valid_signature(Party.SELLER):
                self._execute(quote, version)
            return self.get_quote(quote_id)

    def withdraw(self, quote_id: str, party: Party | str) -> dict:
        """成交前任一方单方撤回：双方冻结在同一锁内一并补偿释放。"""
        with self._lock:
            quote = self._require_quote(quote_id)
            self._expire_if_due(quote)
            if quote.status not in (QuoteStatus.DRAFT, QuoteStatus.OPEN):
                raise StateError(f"当前状态 {quote.status.value} 不可撤回；成交后请走清算补偿")
            party = self._parse_party(party)
            if quote.status is QuoteStatus.OPEN:
                self._void_version(quote, quote.current_version, VersionStatus.VOID, reason=f"{party.value} 撤回")
            else:
                quote.current_version.status = VersionStatus.VOID
            quote.status = QuoteStatus.WITHDRAWN
            self._event(quote_id, "WITHDRAWN", f"{party.value} 单方撤回，全部冻结已补偿释放")
            return self.get_quote(quote_id)

    def expire(self, quote_id: str) -> dict:
        """主动触发单笔超时检查。"""
        with self._lock:
            quote = self._require_quote(quote_id)
            self._expire_if_due(quote)
            return self.get_quote(quote_id)

    def sweep_expired(self) -> list[str]:
        """批量扫描，把超过确认期限的报价置为过期并补偿释放。"""
        with self._lock:
            expired = []
            for quote in self._quotes.values():
                if self._expire_if_due(quote):
                    expired.append(quote.quote_id)
            return expired

    # ------------------------------------------------------------------
    # 清算
    # ------------------------------------------------------------------

    def deliver(self, instruction_id: str, callback_id: str, points: int, cash: int) -> dict:
        """交割回调：支持部分交割；同一 callback_id 重复回调幂等。"""
        with self._lock:
            instruction = self._require_clearing(instruction_id)
            if callback_id in instruction.callbacks:
                self._event(instruction.quote_id, "DUPLICATE_CALLBACK", f"重复回调 {callback_id}，直接返回首次结果")
                return {**instruction.callbacks[callback_id], "duplicate": True}
            if instruction.status not in (ClearingStatus.PENDING, ClearingStatus.PARTIAL):
                raise StateError(f"清算指令状态 {instruction.status.value} 不可交割")
            points, cash = int(points), int(cash)
            if points < 0 or cash < 0 or (points == 0 and cash == 0):
                raise DomainError("交割数量必须为非负且至少一项为正")
            if points > instruction.remaining_points or cash > instruction.remaining_cents:
                raise DomainError(
                    f"交割超出剩余量：剩余积分 {instruction.remaining_points}，剩余资金 {instruction.remaining_cents}"
                )
            escrow = instruction.escrow_owner
            ref = f"{instruction.quote_id}:{instruction_id}:{callback_id}"
            if points:
                self.ledger.debit_available(escrow, Asset.POINT, points, ref, "交割积分出托管")
                self.ledger.credit(instruction.buyer, Asset.POINT, points, ref, "买方收积分")
                instruction.delivered_points += points
            if cash:
                self.ledger.debit_available(escrow, Asset.CASH, cash, ref, "交割资金出托管")
                self.ledger.credit(instruction.seller, Asset.CASH, cash, ref, "卖方收资金")
                instruction.paid_cents += cash
            if instruction.remaining_points == 0 and instruction.remaining_cents == 0:
                instruction.status = ClearingStatus.COMPLETED
            else:
                instruction.status = ClearingStatus.PARTIAL
            result = {
                "instruction_id": instruction_id,
                "callback_id": callback_id,
                "delivered_points": instruction.delivered_points,
                "paid_cents": instruction.paid_cents,
                "status": instruction.status.value,
                "duplicate": False,
            }
            instruction.callbacks[callback_id] = dict(result)
            self._event(
                instruction.quote_id,
                "DELIVERED",
                f"交割回调 {callback_id}：积分 +{points}、资金 +{cash}，状态 {instruction.status.value}",
            )
            return result

    def close_clearing(self, instruction_id: str) -> dict:
        """关闭清算指令：未交割余量以补偿分录退回原主（部分交割场景）。"""
        with self._lock:
            instruction = self._require_clearing(instruction_id)
            if instruction.status in (ClearingStatus.COMPLETED, ClearingStatus.COMPENSATED):
                raise StateError(f"清算指令已关闭，状态 {instruction.status.value}")
            escrow = instruction.escrow_owner
            ref_prefix = f"{instruction.quote_id}:{instruction_id}"
            if instruction.remaining_points:
                self.ledger.debit_available(escrow, Asset.POINT, instruction.remaining_points, f"{ref_prefix}:close", "余量积分出托管")
                self.ledger.compensate(
                    instruction.seller,
                    Asset.POINT,
                    instruction.remaining_points,
                    f"{ref_prefix}:close",
                    "未交割积分补偿退回卖方",
                    compensates=instruction.settle_entry_ids[Party.SELLER.value],
                )
            if instruction.remaining_cents:
                self.ledger.debit_available(escrow, Asset.CASH, instruction.remaining_cents, f"{ref_prefix}:close", "余量资金出托管")
                self.ledger.compensate(
                    instruction.buyer,
                    Asset.CASH,
                    instruction.remaining_cents,
                    f"{ref_prefix}:close",
                    "未支付资金补偿退回买方",
                    compensates=instruction.settle_entry_ids[Party.BUYER.value],
                )
            instruction.status = ClearingStatus.COMPENSATED
            self._event(
                instruction.quote_id,
                "CLEARING_CLOSED",
                f"清算关闭：补偿退回积分 {instruction.remaining_points}、资金 {instruction.remaining_cents}",
            )
            return self.get_clearing(instruction_id)

    def get_clearing(self, instruction_id: str) -> dict:
        with self._lock:
            instruction = self._require_clearing(instruction_id)
            escrow = instruction.escrow_owner
            return {
                "instruction_id": instruction.instruction_id,
                "quote_id": instruction.quote_id,
                "version_no": instruction.version_no,
                "status": instruction.status.value,
                "points": instruction.points,
                "total_cents": instruction.total_cents,
                "delivered_points": instruction.delivered_points,
                "paid_cents": instruction.paid_cents,
                "remaining_points": instruction.remaining_points,
                "remaining_cents": instruction.remaining_cents,
                "escrow": {
                    asset.value: {
                        "available": self.ledger.account(escrow, asset).available,
                        "frozen": self.ledger.account(escrow, asset).frozen,
                    }
                    for asset in Asset
                },
                "callbacks": list(instruction.callbacks.values()),
                "entries": [e.to_dict() for e in self.ledger.entries_for_ref(f"{instruction.quote_id}:{instruction_id}")],
            }

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------

    def get_quote(self, quote_id: str) -> dict:
        """报价全量视图：每一步签署与资金状态。"""
        with self._lock:
            quote = self._require_quote(quote_id)
            self._expire_if_due(quote)
            return {
                "quote_id": quote.quote_id,
                "status": quote.status.value,
                "buyer": quote.buyer,
                "seller": quote.seller,
                "created_at": quote.created_at,
                "current_version": quote.current_version.version_no,
                "versions": [self._version_view(v) for v in quote.versions],
                "freezes": [
                    {
                        "freeze_id": f.freeze_id,
                        "owner": f.owner,
                        "asset": f.asset.value,
                        "amount": f.amount,
                        "status": f.status.value,
                        "ref": f.ref,
                    }
                    for f in self._freezes.values()
                    if f.ref.startswith(quote_id)
                ],
                "clearing": [
                    c.instruction_id for c in self._clearing.values() if c.quote_id == quote_id
                ],
                "accounts": {
                    "buyer": self.get_account(quote.buyer)["balances"],
                    "seller": self.get_account(quote.seller)["balances"],
                },
                "ledger": [e.to_dict() for e in self.ledger.entries_for_ref(quote_id)],
                "timeline": list(self._timelines[quote_id]),
            }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _execute(self, quote: Quote, version: QuoteVersion) -> None:
        """同一版本双签达成：原子消耗双方冻结，生成清算指令与托管户。"""
        instruction_id = f"CLR-{quote.quote_id}-V{version.version_no}"
        instruction = ClearingInstruction(
            instruction_id=instruction_id,
            quote_id=quote.quote_id,
            version_no=version.version_no,
            buyer=quote.buyer,
            seller=quote.seller,
            points=version.terms.points,
            total_cents=version.terms.total_cents,
            created_at=self._clock(),
        )
        escrow = instruction.escrow_owner
        for party, asset, amount in (
            (Party.SELLER, Asset.POINT, version.terms.points),
            (Party.BUYER, Asset.CASH, version.terms.total_cents),
        ):
            owner = quote.owner_of(party)
            freeze = self._active_freeze(quote.quote_id, version.version_no, party)
            ref = f"{quote.quote_id}:{instruction_id}"
            self.ledger.consume_frozen(owner, asset, amount, ref, f"{party.value} 冻结转入清算托管")
            entry = self.ledger.credit(escrow, asset, amount, ref, "清算托管入账")
            instruction.settle_entry_ids[party.value] = entry.entry_id
            freeze.status = FreezeStatus.CONSUMED
        for sig in version.signatures:
            sig.status = SignatureStatus.CONSUMED
        version.status = VersionStatus.EXECUTED
        quote.status = QuoteStatus.EXECUTED
        self._clearing[instruction_id] = instruction
        self._event(
            quote.quote_id,
            "EXECUTED",
            f"v{version.version_no} 双签达成，原子成交，清算指令 {instruction_id} 已生成",
        )

    def _void_version(self, quote: Quote, version: QuoteVersion, target: VersionStatus, reason: str) -> None:
        """版本作废：签署失效传播 + 双方冻结以补偿分录一并释放。"""
        for sig in version.signatures:
            if sig.status is SignatureStatus.VALID:
                sig.status = SignatureStatus.INVALIDATED
                self._event(quote.quote_id, "SIGNATURE_INVALIDATED", f"{sig.party.value} 对 v{version.version_no} 的签署失效（{reason}）")
        for party in (Party.SELLER, Party.BUYER):
            freeze = self._active_freeze(quote.quote_id, version.version_no, party, required=False)
            if freeze is not None:
                self.ledger.release(
                    freeze.owner,
                    freeze.asset,
                    freeze.amount,
                    freeze.ref,
                    f"{reason}，补偿释放冻结",
                    compensates=freeze.entry_id,
                )
                freeze.status = FreezeStatus.RELEASED
        version.status = target

    def _expire_if_due(self, quote: Quote) -> bool:
        """确认期限已过仍未成交：置为过期并补偿释放（幂等）。"""
        if quote.status is not QuoteStatus.OPEN:
            return False
        version = quote.current_version
        if version.confirm_deadline is None or self._clock() <= version.confirm_deadline:
            return False
        self._void_version(quote, version, VersionStatus.VOID, reason="确认期限已过")
        quote.status = QuoteStatus.EXPIRED
        self._event(quote.quote_id, "EXPIRED", f"v{version.version_no} 超过确认期限，报价过期，冻结已补偿释放")
        return True

    def _freeze_pair(self, quote: Quote, version: QuoteVersion) -> None:
        """额度双向冻结：卖方冻积分、买方冻资金。"""
        for party, asset, amount in (
            (Party.SELLER, Asset.POINT, version.terms.points),
            (Party.BUYER, Asset.CASH, version.terms.total_cents),
        ):
            owner = quote.owner_of(party)
            ref = f"{quote.quote_id}:v{version.version_no}:{party.value}"
            entry = self.ledger.freeze(owner, asset, amount, ref, f"v{version.version_no} {party.value} 额度冻结")
            freeze_id = f"FZ-{quote.quote_id}-V{version.version_no}-{party.value}"
            self._freezes[freeze_id] = Freeze(
                freeze_id=freeze_id,
                owner=owner,
                asset=asset,
                amount=amount,
                ref=ref,
                entry_id=entry.entry_id,
            )

    def _active_freeze(self, quote_id: str, version_no: int, party: Party, required: bool = True) -> Freeze | None:
        freeze = self._freezes.get(f"FZ-{quote_id}-V{version_no}-{party.value}")
        if freeze is None or freeze.status is not FreezeStatus.ACTIVE:
            if required:
                raise StateError(f"缺少 {party.value} 在 v{version_no} 的有效冻结")
            return None
        return freeze

    def _require_quote(self, quote_id: str) -> Quote:
        try:
            return self._quotes[quote_id]
        except KeyError:
            raise NotFoundError(f"报价不存在：{quote_id}") from None

    def _require_clearing(self, instruction_id: str) -> ClearingInstruction:
        try:
            return self._clearing[instruction_id]
        except KeyError:
            raise NotFoundError(f"清算指令不存在：{instruction_id}") from None

    @staticmethod
    def _parse_party(party: Party | str) -> Party:
        try:
            return party if isinstance(party, Party) else Party(str(party).upper())
        except ValueError:
            raise DomainError(f"未知参与方：{party}，应为 BUYER 或 SELLER") from None

    @staticmethod
    def _validate_terms(points: int, unit_price_cents: int) -> Terms:
        terms = Terms(points=int(points), unit_price_cents=int(unit_price_cents))
        if terms.points <= 0 or terms.unit_price_cents <= 0:
            raise DomainError("积分数量与单价必须为正")
        return terms

    def _version_view(self, version: QuoteVersion) -> dict:
        return {
            "version_no": version.version_no,
            "status": version.status.value,
            "terms": {
                "points": version.terms.points,
                "unit_price_cents": version.terms.unit_price_cents,
                "total_cents": version.terms.total_cents,
            },
            "published_at": version.published_at,
            "confirm_deadline": version.confirm_deadline,
            "signatures": [
                {"party": s.party.value, "status": s.status.value, "signed_at": s.signed_at}
                for s in version.signatures
            ],
        }

    def _event(self, quote_id: str, kind: str, summary: str) -> None:
        self._event_seq += 1
        self._timelines[quote_id].append(
            {"seq": self._event_seq, "at": self._clock(), "kind": kind, "summary": summary}
        )
