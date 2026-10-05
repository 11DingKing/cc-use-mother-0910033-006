"""双边成交确认核心服务。

状态机约定：
- 报价仅在 OPEN 状态下可签署、修订、撤回；
- 同一版本集齐买卖双方签署后原子成交（EXECUTED），生成清算指令；
- 条款修订使旧版签署失效，并以补偿分录释放对应冻结；
- 单方撤回、确认期限超时同样以补偿分录释放冻结；
- 清算回调支持部分交割，缺口以补偿分录回补；回调按 callback_id 幂等去重。
"""
from __future__ import annotations

import itertools
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from .errors import (
    CallbackConflict,
    InsufficientQuota,
    InvalidInput,
    InvalidState,
    NotAParty,
    NotFound,
    QuoteExpired,
    VersionMismatch,
)
from .ledger import Ledger, account_of, system_account
from .models import (
    Asset,
    Direction,
    EntryKind,
    Event,
    LedgerEntry,
    LedgerLine,
    LegStatus,
    Quote,
    QuoteStatus,
    QuoteTerms,
    QuoteVersion,
    Settlement,
    SettlementLeg,
    SettlementStatus,
    Side,
    Signature,
    SignatureStatus,
    _iso,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_positive(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise InvalidInput(f"{field} 必须是正整数")
    return value


def _require_non_negative(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidInput(f"{field} 必须是非负整数")
    return value


def _require_party(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput("交易方名称必须是非空字符串")
    name = value.strip()
    if ":" in name:
        raise InvalidInput("交易方名称不能包含冒号")
    return name


class TradeConfirmationService:
    """管理报价全生命周期：同版双签原子成交，异常路径以补偿分录收尾。"""

    def __init__(self, clock: Callable[[], datetime] = utc_now):
        self._clock = clock
        self._lock = threading.RLock()
        self._ledger = Ledger()
        self._quotes: dict[str, Quote] = {}
        self._settlements: dict[str, Settlement] = {}
        self._quote_seq = itertools.count(1)
        self._entry_seq = itertools.count(1)

    # ------------------------------------------------------------------
    # 额度
    # ------------------------------------------------------------------
    def deposit(self, party: str, asset: Asset, amount: int) -> dict:
        """初始额度入账（对端为系统账户）。"""
        with self._lock:
            party = _require_party(party)
            amount = _require_positive(amount, "amount")
            self._post(
                quote_id=None,
                kind=EntryKind.DEPOSIT,
                reason="初始额度入账",
                lines=[
                    LedgerLine(account_of(party, asset, "available"), asset, Direction.DEBIT, amount),
                    LedgerLine(system_account(asset), asset, Direction.CREDIT, amount),
                ],
                correlation_id=f"DEPOSIT:{party}:{asset.value}",
            )
            return {"party": party, "fund_status": self.fund_status(party)}

    def fund_status(self, party: str) -> dict:
        """某主体在两类资产上的可用/冻结/在途余额。"""
        with self._lock:
            return {
                asset.value: {
                    bucket: self._ledger.balance(account_of(party, asset, bucket))
                    for bucket in ("available", "frozen", "in_transit")
                }
                for asset in Asset
            }

    def account_detail(self, party: str) -> dict:
        with self._lock:
            return {
                "party": party,
                "fund_status": self.fund_status(party),
                "entries": [entry.to_dict() for entry in self._ledger.entries_for_party(party)],
            }

    # ------------------------------------------------------------------
    # 报价生命周期
    # ------------------------------------------------------------------
    def create_quote(
        self,
        *,
        seller: str,
        buyer: str,
        points: int,
        price: int,
        confirm_seconds: int,
        note: str = "",
        quote_id: str | None = None,
    ) -> dict:
        """创建报价（版本 v1），进入待双方确认状态。"""
        with self._lock:
            seller = _require_party(seller)
            buyer = _require_party(buyer)
            if seller == buyer:
                raise InvalidInput("买卖双方不能为同一主体")
            points = _require_positive(points, "points")
            price = _require_positive(price, "price")
            confirm_seconds = _require_positive(confirm_seconds, "confirm_seconds")
            qid = quote_id or f"Q-{next(self._quote_seq):06d}"
            if qid in self._quotes:
                raise InvalidInput(f"报价编号已存在：{qid}")
            now = self._clock()
            version = QuoteVersion(
                version_no=1,
                terms=QuoteTerms(points=points, price=price),
                created_by=seller,
                created_at=now,
                confirm_deadline=now + timedelta(seconds=confirm_seconds),
                note=note,
            )
            quote = Quote(
                quote_id=qid,
                seller=seller,
                buyer=buyer,
                status=QuoteStatus.OPEN,
                versions=[version],
                signatures=[],
                events=[],
                created_at=now,
            )
            self._quotes[qid] = quote
            self._emit(
                quote,
                "QUOTE_CREATED",
                seller,
                {"terms": version.terms.to_dict(), "confirm_deadline": _iso(version.confirm_deadline)},
            )
            return self.quote_detail(qid)

    def sign(self, quote_id: str, *, party: str, version: int) -> dict:
        """一方签署指定版本；同版双签集齐即原子成交。"""
        with self._lock:
            quote = self._load_quote(quote_id)
            self._require_open(quote)
            side = self._side_of(quote, party)
            current = quote.current_version
            version = _require_positive(version, "version")
            if version != current.version_no:
                raise VersionMismatch(
                    f"签署版本 v{version} 与当前条款版本 v{current.version_no} 不一致，"
                    "请基于最新条款重新确认"
                )
            if quote.active_signature_for(side) is not None:
                return self.quote_detail(quote_id)  # 幂等：同方同版重复签署
            terms = current.terms
            asset, amount = self._freeze_requirement(quote, side, terms)
            available = self._ledger.balance(account_of(party, asset, "available"))
            if available < amount:
                raise InsufficientQuota(
                    f"{party} 的 {asset.value} 可用额度不足：需冻结 {amount}，现有 {available}"
                )
            entry = self._post(
                quote_id=quote_id,
                kind=EntryKind.FREEZE,
                reason=f"v{current.version_no} 签署冻结",
                lines=[
                    LedgerLine(account_of(party, asset, "frozen"), asset, Direction.DEBIT, amount),
                    LedgerLine(account_of(party, asset, "available"), asset, Direction.CREDIT, amount),
                ],
                correlation_id=f"{quote_id}:v{current.version_no}:{side.value}",
            )
            quote.signatures.append(
                Signature(side=side, party=party, version=current.version_no, signed_at=self._clock())
            )
            self._emit(
                quote,
                "SIGNED",
                party,
                {
                    "version": current.version_no,
                    "side": side.value,
                    "asset": asset.value,
                    "amount": amount,
                    "freeze_entry": entry.entry_id,
                },
            )
            if quote.active_signature_for(Side.SELL) and quote.active_signature_for(Side.BUY):
                self._execute(quote, current)
            return self.quote_detail(quote_id)

    def revise(
        self,
        quote_id: str,
        *,
        party: str,
        points: int | None = None,
        price: int | None = None,
        confirm_seconds: int | None = None,
        note: str = "",
    ) -> dict:
        """修订条款：生成新版本，旧版签署失效并释放冻结（补偿分录）。"""
        with self._lock:
            quote = self._load_quote(quote_id)
            self._require_open(quote)
            self._side_of(quote, party)
            current = quote.current_version
            new_terms = QuoteTerms(
                points=_require_positive(points, "points") if points is not None else current.terms.points,
                price=_require_positive(price, "price") if price is not None else current.terms.price,
            )
            if confirm_seconds is not None:
                confirm_seconds = _require_positive(confirm_seconds, "confirm_seconds")
            self._invalidate_signatures(quote, reason="条款修订，旧版签署失效")
            now = self._clock()
            deadline = (
                now + timedelta(seconds=confirm_seconds)
                if confirm_seconds is not None
                else current.confirm_deadline
            )
            version = QuoteVersion(
                version_no=current.version_no + 1,
                terms=new_terms,
                created_by=party,
                created_at=now,
                confirm_deadline=deadline,
                note=note,
            )
            quote.versions.append(version)
            self._emit(
                quote,
                "QUOTE_REVISED",
                party,
                {"version": version.version_no, "terms": version.terms.to_dict(), "note": note},
            )
            return self.quote_detail(quote_id)

    def withdraw(self, quote_id: str, *, party: str, reason: str = "") -> dict:
        """单方撤回：未成交前任意一方可撤回，冻结额度以补偿分录释放。"""
        with self._lock:
            quote = self._load_quote(quote_id)
            self._side_of(quote, party)
            self._refresh(quote)
            if quote.status is QuoteStatus.EXPIRED:
                raise QuoteExpired(f"报价 {quote_id} 已过确认期限，按超时处理")
            if quote.status is not QuoteStatus.OPEN:
                raise InvalidState(
                    f"报价 {quote_id} 处于 {quote.status.value}，不可单方撤回；"
                    "已成交报价只能经清算补偿收尾"
                )
            label = "单方撤回" + (f"：{reason}" if reason else "")
            self._invalidate_signatures(quote, reason=label)
            quote.status = QuoteStatus.WITHDRAWN
            self._emit(quote, "WITHDRAWN", party, {"reason": reason})
            return self.quote_detail(quote_id)

    def expire(self, quote_id: str) -> dict:
        """检查并按需将单个报价置为超时。"""
        with self._lock:
            quote = self._load_quote(quote_id)
            self._refresh(quote)
            return self.quote_detail(quote_id)

    def sweep_expired(self) -> list[str]:
        """批量处理确认期限已过的报价，返回本次置为超时的报价编号。"""
        with self._lock:
            expired = []
            for quote in self._quotes.values():
                was_open = quote.status is QuoteStatus.OPEN
                self._refresh(quote)
                if was_open and quote.status is QuoteStatus.EXPIRED:
                    expired.append(quote.quote_id)
            return expired

    # ------------------------------------------------------------------
    # 清算
    # ------------------------------------------------------------------
    def settlement_callback(
        self,
        settlement_id: str,
        *,
        callback_id: str,
        asset: Asset,
        delivered: int,
        final: bool = False,
    ) -> dict:
        """清算所交割回调。

        - delivered 为该腿累计交割量，按增量落在途→对手方可用；
        - final=True 且仍有缺口时，缺口以补偿分录回补付款方；
        - callback_id 幂等去重，重复回调不产生重复分录。
        """
        with self._lock:
            settlement = self._settlements.get(settlement_id)
            if settlement is None:
                raise NotFound(f"清算指令不存在：{settlement_id}")
            if callback_id in settlement.processed_callbacks:
                return {"deduplicated": True, "settlement": settlement.to_dict()}
            leg = settlement.legs[asset.value]
            if leg.status in (LegStatus.FULL, LegStatus.SHORT_FINAL):
                raise CallbackConflict(f"{asset.value} 腿已终态（{leg.status.value}），拒绝新的交割回调")
            delivered = _require_non_negative(delivered, "delivered")
            if delivered < leg.delivered:
                raise CallbackConflict(
                    f"累计交割量回退：已记录 {leg.delivered}，回调上报 {delivered}"
                )
            if delivered > leg.expected:
                raise CallbackConflict(
                    f"累计交割量超出应付：应付 {leg.expected}，回调上报 {delivered}"
                )
            quote = self._quotes[settlement.quote_id]
            delta = delivered - leg.delivered
            if delta:
                entry = self._post(
                    quote_id=settlement.quote_id,
                    kind=EntryKind.DELIVER,
                    reason=f"{asset.value} 腿交割 {delta}",
                    lines=[
                        LedgerLine(account_of(leg.to_party, asset, "available"), asset, Direction.DEBIT, delta),
                        LedgerLine(account_of(leg.from_party, asset, "in_transit"), asset, Direction.CREDIT, delta),
                    ],
                    correlation_id=callback_id,
                )
                leg.delivered = delivered
                self._emit(
                    quote,
                    "DELIVERY_CONFIRMED",
                    "clearing",
                    {
                        "asset": asset.value,
                        "delta": delta,
                        "delivered": delivered,
                        "callback_id": callback_id,
                        "deliver_entry": entry.entry_id,
                    },
                )
            if delivered == leg.expected:
                leg.status = LegStatus.FULL
            elif final:
                shortfall = leg.remaining
                entry = self._post(
                    quote_id=settlement.quote_id,
                    kind=EntryKind.COMPENSATE,
                    reason=f"{asset.value} 腿部分交割，缺口 {shortfall} 补偿回补",
                    lines=[
                        LedgerLine(account_of(leg.from_party, asset, "available"), asset, Direction.DEBIT, shortfall),
                        LedgerLine(account_of(leg.from_party, asset, "in_transit"), asset, Direction.CREDIT, shortfall),
                    ],
                    correlation_id=callback_id,
                )
                leg.status = LegStatus.SHORT_FINAL
                self._emit(
                    quote,
                    "COMPENSATION_POSTED",
                    "clearing",
                    {
                        "asset": asset.value,
                        "shortfall": shortfall,
                        "callback_id": callback_id,
                        "compensate_entry": entry.entry_id,
                    },
                )
            else:
                leg.status = LegStatus.DELIVERING
            settlement.processed_callbacks.append(callback_id)
            self._close_if_terminal(settlement)
            return {"deduplicated": False, "settlement": settlement.to_dict()}

    def settlement_detail(self, settlement_id: str) -> dict:
        with self._lock:
            settlement = self._settlements.get(settlement_id)
            if settlement is None:
                raise NotFound(f"清算指令不存在：{settlement_id}")
            return settlement.to_dict()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def quote_detail(self, quote_id: str) -> dict:
        """报价全量视图：版本、签署、清算、分录、时间线与双方资金状态。"""
        with self._lock:
            quote = self._load_quote(quote_id)
            self._refresh(quote)
            settlement = (
                self._settlements.get(quote.settlement_id) if quote.settlement_id else None
            )
            return {
                "quote_id": quote.quote_id,
                "status": quote.status.value,
                "seller": quote.seller,
                "buyer": quote.buyer,
                "current_version": quote.current_version.version_no,
                "created_at": _iso(quote.created_at),
                "versions": [version.to_dict() for version in quote.versions],
                "signatures": [sig.to_dict() for sig in quote.signatures],
                "settlement": settlement.to_dict() if settlement else None,
                "ledger": [entry.to_dict() for entry in self._ledger.entries_for_quote(quote_id)],
                "timeline": [event.to_dict() for event in quote.events],
                "fund_status": {
                    quote.seller: self.fund_status(quote.seller),
                    quote.buyer: self.fund_status(quote.buyer),
                },
            }

    def list_quotes(self) -> list[dict]:
        with self._lock:
            for quote in self._quotes.values():
                self._refresh(quote)
            return [quote.summary() for quote in self._quotes.values()]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _load_quote(self, quote_id: str) -> Quote:
        quote = self._quotes.get(quote_id)
        if quote is None:
            raise NotFound(f"报价不存在：{quote_id}")
        return quote

    @staticmethod
    def _side_of(quote: Quote, party: str) -> Side:
        if party == quote.seller:
            return Side.SELL
        if party == quote.buyer:
            return Side.BUY
        raise NotAParty(f"{party} 不是报价 {quote.quote_id} 的交易方")

    @staticmethod
    def _freeze_requirement(quote: Quote, side: Side, terms: QuoteTerms) -> tuple[Asset, int]:
        """卖方冻结积分、买方冻结资金，实现额度双向冻结。"""
        if side is Side.SELL:
            return Asset.POINTS, terms.points
        return Asset.CASH, terms.price

    def _require_open(self, quote: Quote) -> None:
        self._refresh(quote)
        if quote.status is QuoteStatus.EXPIRED:
            raise QuoteExpired(f"报价 {quote.quote_id} 已过确认期限，按超时处理")
        if quote.status is not QuoteStatus.OPEN:
            raise InvalidState(f"报价 {quote.quote_id} 当前状态 {quote.status.value}，不允许该操作")

    def _refresh(self, quote: Quote) -> None:
        """惰性超时：确认期限已过的 OPEN 报价立即按超时收尾。"""
        if quote.status is QuoteStatus.OPEN and self._clock() > quote.current_version.confirm_deadline:
            self._expire(quote)

    def _expire(self, quote: Quote) -> None:
        self._invalidate_signatures(quote, reason="确认期限超时")
        quote.status = QuoteStatus.EXPIRED
        self._emit(
            quote,
            "EXPIRED",
            "system",
            {"deadline": _iso(quote.current_version.confirm_deadline)},
        )

    def _invalidate_signatures(self, quote: Quote, *, reason: str) -> None:
        """签署失效传播：所有有效签署置为失效，冻结额度以补偿分录释放。"""
        for sig in quote.signatures:
            if sig.status is not SignatureStatus.ACTIVE:
                continue
            sig.status = SignatureStatus.INVALIDATED
            sig.invalidated_reason = reason
            terms = next(v.terms for v in quote.versions if v.version_no == sig.version)
            asset, amount = self._freeze_requirement(quote, sig.side, terms)
            entry = self._post(
                quote_id=quote.quote_id,
                kind=EntryKind.RELEASE,
                reason=f"{reason}，释放 v{sig.version} 冻结",
                lines=[
                    LedgerLine(account_of(sig.party, asset, "available"), asset, Direction.DEBIT, amount),
                    LedgerLine(account_of(sig.party, asset, "frozen"), asset, Direction.CREDIT, amount),
                ],
                correlation_id=f"{quote.quote_id}:v{sig.version}:{sig.side.value}:release",
            )
            self._emit(
                quote,
                "SIGNATURE_INVALIDATED",
                sig.party,
                {
                    "version": sig.version,
                    "side": sig.side.value,
                    "reason": reason,
                    "release_entry": entry.entry_id,
                },
            )

    def _execute(self, quote: Quote, version: QuoteVersion) -> None:
        """同版双签达成：一条分录内双方冻结同时转在途，原子成交。"""
        terms = version.terms
        entry = self._post(
            quote_id=quote.quote_id,
            kind=EntryKind.SETTLE,
            reason="同版双签达成，原子成交：双方冻结同时转在途",
            lines=[
                LedgerLine(account_of(quote.seller, Asset.POINTS, "in_transit"), Asset.POINTS, Direction.DEBIT, terms.points),
                LedgerLine(account_of(quote.seller, Asset.POINTS, "frozen"), Asset.POINTS, Direction.CREDIT, terms.points),
                LedgerLine(account_of(quote.buyer, Asset.CASH, "in_transit"), Asset.CASH, Direction.DEBIT, terms.price),
                LedgerLine(account_of(quote.buyer, Asset.CASH, "frozen"), Asset.CASH, Direction.CREDIT, terms.price),
            ],
            correlation_id=f"{quote.quote_id}:v{version.version_no}:settle",
        )
        settlement = Settlement(
            settlement_id=f"S-{quote.quote_id}",
            quote_id=quote.quote_id,
            version=version.version_no,
            legs={
                Asset.POINTS.value: SettlementLeg(
                    asset=Asset.POINTS, from_party=quote.seller, to_party=quote.buyer, expected=terms.points
                ),
                Asset.CASH.value: SettlementLeg(
                    asset=Asset.CASH, from_party=quote.buyer, to_party=quote.seller, expected=terms.price
                ),
            },
            status=SettlementStatus.PENDING,
            created_at=self._clock(),
        )
        self._settlements[settlement.settlement_id] = settlement
        quote.settlement_id = settlement.settlement_id
        quote.status = QuoteStatus.EXECUTED
        self._emit(
            quote,
            "EXECUTED",
            "system",
            {
                "version": version.version_no,
                "settlement_id": settlement.settlement_id,
                "settle_entry": entry.entry_id,
            },
        )

    def _close_if_terminal(self, settlement: Settlement) -> None:
        statuses = {leg.status for leg in settlement.legs.values()}
        if statuses <= {LegStatus.FULL, LegStatus.SHORT_FINAL}:
            settlement.status = (
                SettlementStatus.SETTLED
                if statuses == {LegStatus.FULL}
                else SettlementStatus.COMPLETED_WITH_COMPENSATION
            )
            quote = self._quotes[settlement.quote_id]
            quote.status = QuoteStatus.CLOSED
            self._emit(quote, "CLOSED", "system", {"settlement_status": settlement.status.value})

    def _post(
        self,
        *,
        quote_id: str | None,
        kind: EntryKind,
        reason: str,
        lines: list[LedgerLine],
        correlation_id: str,
    ) -> LedgerEntry:
        entry = LedgerEntry(
            entry_id=f"E-{next(self._entry_seq):06d}",
            quote_id=quote_id,
            kind=kind,
            reason=reason,
            lines=tuple(lines),
            created_at=self._clock(),
            correlation_id=correlation_id,
        )
        self._ledger.post(entry)
        return entry

    def _emit(self, quote: Quote, type_: str, actor: str, detail: dict) -> None:
        quote.events.append(
            Event(seq=len(quote.events) + 1, type=type_, at=self._clock(), actor=actor, detail=detail)
        )
