"""领域模型：报价版本、双方签署、清算指令与台账分录。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


def _iso(value: datetime) -> str:
    return value.isoformat()


class Asset(str, enum.Enum):
    """清算涉及的两类标的。"""

    POINTS = "POINTS"  # 积分
    CASH = "CASH"  # 资金


class Side(str, enum.Enum):
    """交易方向。"""

    SELL = "SELL"  # 卖方：出让积分、收取资金
    BUY = "BUY"  # 买方：支付资金、收取积分


class QuoteStatus(str, enum.Enum):
    """报价生命周期。"""

    OPEN = "OPEN"  # 待双方确认
    EXECUTED = "EXECUTED"  # 同版双签达成，已原子成交，待清算
    WITHDRAWN = "WITHDRAWN"  # 单方撤回
    EXPIRED = "EXPIRED"  # 确认期限超时
    CLOSED = "CLOSED"  # 清算完成（含补偿分录收尾）


class SignatureStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"  # 签署失效传播：条款修订、撤回或超时


class EntryKind(str, enum.Enum):
    """台账分录类型；RELEASE 与 COMPENSATE 均为补偿分录。"""

    DEPOSIT = "DEPOSIT"  # 初始额度入账
    FREEZE = "FREEZE"  # 签署时冻结额度
    RELEASE = "RELEASE"  # 补偿分录：签署失效后释放冻结
    SETTLE = "SETTLE"  # 原子成交：双方冻结同时转在途
    DELIVER = "DELIVER"  # 清算交割：在途转对手方可用
    COMPENSATE = "COMPENSATE"  # 补偿分录：部分交割缺口回补


COMPENSATING_KINDS = frozenset({EntryKind.RELEASE, EntryKind.COMPENSATE})


class Direction(str, enum.Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class LegStatus(str, enum.Enum):
    PENDING = "PENDING"
    DELIVERING = "DELIVERING"  # 部分交割中
    FULL = "FULL"  # 全额交割
    SHORT_FINAL = "SHORT_FINAL"  # 终态不足额，缺口已补偿


class SettlementStatus(str, enum.Enum):
    PENDING = "PENDING"
    SETTLED = "SETTLED"  # 双腿全额交割
    COMPLETED_WITH_COMPENSATION = "COMPLETED_WITH_COMPENSATION"  # 含缺口补偿收尾


@dataclass(frozen=True)
class QuoteTerms:
    """报价条款：积分数量与资金总额（最小单位整数）。"""

    points: int
    price: int

    def to_dict(self) -> dict:
        return {"points": self.points, "price": self.price}


@dataclass(frozen=True)
class QuoteVersion:
    """条款版本：每次修订生成新版本，旧版签署随之失效。"""

    version_no: int
    terms: QuoteTerms
    created_by: str
    created_at: datetime
    confirm_deadline: datetime
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "version_no": self.version_no,
            "terms": self.terms.to_dict(),
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
            "confirm_deadline": _iso(self.confirm_deadline),
            "note": self.note,
        }


@dataclass
class Signature:
    """一方对某一版本条款的签署。"""

    side: Side
    party: str
    version: int
    signed_at: datetime
    status: SignatureStatus = SignatureStatus.ACTIVE
    invalidated_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "side": self.side.value,
            "party": self.party,
            "version": self.version,
            "signed_at": _iso(self.signed_at),
            "status": self.status.value,
            "invalidated_reason": self.invalidated_reason,
        }


@dataclass(frozen=True)
class LedgerLine:
    """分录行：某账户某资产的一次借贷。"""

    account: str
    asset: Asset
    direction: Direction
    amount: int

    def to_dict(self) -> dict:
        return {
            "account": self.account,
            "asset": self.asset.value,
            "direction": self.direction.value,
            "amount": self.amount,
        }


@dataclass(frozen=True)
class LedgerEntry:
    """一条平衡分录：同一资产借贷相等。"""

    entry_id: str
    quote_id: str | None
    kind: EntryKind
    reason: str
    lines: tuple[LedgerLine, ...]
    created_at: datetime
    correlation_id: str

    @property
    def compensating(self) -> bool:
        return self.kind in COMPENSATING_KINDS

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "quote_id": self.quote_id,
            "kind": self.kind.value,
            "compensating": self.compensating,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "created_at": _iso(self.created_at),
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass(frozen=True)
class Event:
    """报价时间线上的一步。"""

    seq: int
    type: str
    at: datetime
    actor: str
    detail: dict

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "type": self.type,
            "at": _iso(self.at),
            "actor": self.actor,
            "detail": self.detail,
        }


@dataclass
class SettlementLeg:
    """清算指令的一条腿：某资产从一方到另一方的交割。"""

    asset: Asset
    from_party: str
    to_party: str
    expected: int
    delivered: int = 0
    status: LegStatus = LegStatus.PENDING

    @property
    def remaining(self) -> int:
        return self.expected - self.delivered

    def to_dict(self) -> dict:
        return {
            "asset": self.asset.value,
            "from_party": self.from_party,
            "to_party": self.to_party,
            "expected": self.expected,
            "delivered": self.delivered,
            "remaining": self.remaining,
            "status": self.status.value,
        }


@dataclass
class Settlement:
    """原子成交后生成的清算指令，双腿分别跟踪交割进度。"""

    settlement_id: str
    quote_id: str
    version: int
    legs: dict[str, SettlementLeg]
    status: SettlementStatus
    created_at: datetime
    processed_callbacks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "settlement_id": self.settlement_id,
            "quote_id": self.quote_id,
            "version": self.version,
            "status": self.status.value,
            "legs": {key: leg.to_dict() for key, leg in self.legs.items()},
            "processed_callbacks": list(self.processed_callbacks),
            "created_at": _iso(self.created_at),
        }


@dataclass
class Quote:
    """一份待双边确认的报价。"""

    quote_id: str
    seller: str
    buyer: str
    status: QuoteStatus
    versions: list[QuoteVersion]
    signatures: list[Signature]
    events: list[Event]
    created_at: datetime
    settlement_id: str | None = None

    @property
    def current_version(self) -> QuoteVersion:
        return self.versions[-1]

    def active_signature_for(self, side: Side) -> Signature | None:
        for sig in reversed(self.signatures):
            if sig.side is side and sig.status is SignatureStatus.ACTIVE:
                return sig
        return None

    def summary(self) -> dict:
        return {
            "quote_id": self.quote_id,
            "status": self.status.value,
            "seller": self.seller,
            "buyer": self.buyer,
            "current_version": self.current_version.version_no,
            "settlement_id": self.settlement_id,
            "created_at": _iso(self.created_at),
        }
