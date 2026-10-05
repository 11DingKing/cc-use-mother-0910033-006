"""双边成交确认的领域模型。

对齐 domain/contract.json 中的四条不变量：
- 双边同版签署：只有同一报价版本集齐买卖双方签署才允许成交；
- 额度双向冻结：版本生效即同时冻结卖方积分与买方资金；
- 签署失效传播：条款修订使旧版本签署失效，并联动释放对应冻结；
- 撤销补偿分录：撤回、超时、部分交割的余量都以补偿分录冲回。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Party(str, enum.Enum):
    """成交双方角色。"""

    BUYER = "BUYER"
    SELLER = "SELLER"

    @property
    def counterpart(self) -> "Party":
        return Party.SELLER if self is Party.BUYER else Party.BUYER


class Asset(str, enum.Enum):
    """冻结与清算涉及的资产。"""

    POINT = "POINT"  # 积分（个）
    CASH = "CASH"  # 资金（分）


class QuoteStatus(str, enum.Enum):
    """报价生命周期。"""

    DRAFT = "DRAFT"  # 草稿：已录入条款，尚未冻结与发布
    OPEN = "OPEN"  # 协商中：版本已发布，等待双签
    EXECUTED = "EXECUTED"  # 已成交：同一版本双签达成，原子划转完成
    WITHDRAWN = "WITHDRAWN"  # 已撤回：任一方在成交前单方撤回
    EXPIRED = "EXPIRED"  # 已过期：确认期限内未集齐双签


class VersionStatus(str, enum.Enum):
    """报价版本状态。"""

    DRAFT = "DRAFT"  # 已录入未发布
    ACTIVE = "ACTIVE"  # 当前有效版本，可签署
    SUPERSEDED = "SUPERSEDED"  # 被修订产生的新版本取代
    EXECUTED = "EXECUTED"  # 该版本已成交
    VOID = "VOID"  # 随报价撤回或过期作废


class SignatureStatus(str, enum.Enum):
    """签署状态。"""

    VALID = "VALID"  # 有效
    CONSUMED = "CONSUMED"  # 成交时被消耗
    INVALIDATED = "INVALIDATED"  # 因修订、撤回或过期失效


class FreezeStatus(str, enum.Enum):
    """额度冻结状态。"""

    ACTIVE = "ACTIVE"  # 冻结中
    RELEASED = "RELEASED"  # 已释放（补偿分录冲回）
    CONSUMED = "CONSUMED"  # 成交时被消耗，转入清算托管


class EntryType(str, enum.Enum):
    """台账分录类型。"""

    DEPOSIT = "DEPOSIT"  # 注资（初始化/测试）
    FREEZE = "FREEZE"  # 冻结：可用转冻结
    RELEASE = "RELEASE"  # 释放：冻结转回可用（补偿分录）
    SETTLE = "SETTLE"  # 清算划转
    COMPENSATE = "COMPENSATE"  # 补偿分录：冲回未交割余量


class ClearingStatus(str, enum.Enum):
    """清算指令状态。"""

    PENDING = "PENDING"  # 待交割
    PARTIAL = "PARTIAL"  # 部分交割
    COMPLETED = "COMPLETED"  # 全额交割完成
    COMPENSATED = "COMPENSATED"  # 关闭时余量已补偿回原主


@dataclass(frozen=True)
class Terms:
    """报价条款。金额一律用整数（积分个、资金分），避免浮点误差。"""

    points: int  # 积分数量
    unit_price_cents: int  # 单价（分/积分）

    @property
    def total_cents(self) -> int:
        return self.points * self.unit_price_cents


@dataclass
class Signature:
    """一方对某个报价版本的签署。"""

    party: Party
    version_no: int
    signed_at: float
    status: SignatureStatus = SignatureStatus.VALID


@dataclass
class QuoteVersion:
    """报价的一个版本。每次条款修订产生新版本。"""

    version_no: int
    terms: Terms
    confirm_seconds: float
    status: VersionStatus = VersionStatus.DRAFT
    published_at: float | None = None
    confirm_deadline: float | None = None
    signatures: list[Signature] = field(default_factory=list)

    def signature_of(self, party: Party) -> Signature | None:
        for sig in self.signatures:
            if sig.party is party:
                return sig
        return None

    def has_valid_signature(self, party: Party) -> bool:
        sig = self.signature_of(party)
        return sig is not None and sig.status is SignatureStatus.VALID


@dataclass
class Quote:
    """一笔场外积分协商报价。"""

    quote_id: str
    buyer: str
    seller: str
    created_at: float
    status: QuoteStatus = QuoteStatus.DRAFT
    versions: list[QuoteVersion] = field(default_factory=list)

    @property
    def current_version(self) -> QuoteVersion:
        return self.versions[-1]

    def owner_of(self, party: Party) -> str:
        return self.buyer if party is Party.BUYER else self.seller


@dataclass
class Freeze:
    """一条额度冻结记录。"""

    freeze_id: str
    owner: str
    asset: Asset
    amount: int
    ref: str  # 关联对象，形如 Q-0001:v2:SELLER
    entry_id: str  # 对应的 FREEZE 台账分录
    status: FreezeStatus = FreezeStatus.ACTIVE


@dataclass
class ClearingInstruction:
    """清算指令：成交后驱动积分与资金的实际交割。"""

    instruction_id: str
    quote_id: str
    version_no: int
    buyer: str
    seller: str
    points: int
    total_cents: int
    created_at: float
    status: ClearingStatus = ClearingStatus.PENDING
    delivered_points: int = 0
    paid_cents: int = 0
    # 成交时把双方冻结转入托管户的 SETTLE 分录，供补偿分录引用
    settle_entry_ids: dict[str, str] = field(default_factory=dict)
    # 已处理的交割回调：callback_id -> 处理结果（幂等去重）
    callbacks: dict[str, dict] = field(default_factory=dict)

    @property
    def escrow_owner(self) -> str:
        return f"escrow:{self.instruction_id}"

    @property
    def remaining_points(self) -> int:
        return self.points - self.delivered_points

    @property
    def remaining_cents(self) -> int:
        return self.total_cents - self.paid_cents
