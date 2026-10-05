"""双边成交确认核心状态机的回归测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trade_confirmation.errors import (
    CallbackConflict,
    InsufficientQuota,
    InvalidState,
    NotAParty,
    QuoteExpired,
    VersionMismatch,
)
from trade_confirmation.models import Asset
from trade_confirmation.service import TradeConfirmationService

SELLER = "sellerA"
BUYER = "buyerB"


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class BilateralTradeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.service = TradeConfirmationService(clock=self.clock)
        self.service.deposit(SELLER, Asset.POINTS, 1_000_000)
        self.service.deposit(BUYER, Asset.CASH, 500_000)

    def _create(self, points: int = 1000, price: int = 20000, ttl: int = 3600) -> str:
        detail = self.service.create_quote(
            seller=SELLER, buyer=BUYER, points=points, price=price, confirm_seconds=ttl
        )
        return detail["quote_id"]

    def _execute(self, qid: str, version: int = 1) -> dict:
        self.service.sign(qid, party=SELLER, version=version)
        return self.service.sign(qid, party=BUYER, version=version)

    # ------------------------------------------------------------------
    # 同版双签原子成交
    # ------------------------------------------------------------------
    def test_same_version_dual_sign_executes_atomically(self) -> None:
        qid = self._create()
        detail = self.service.sign(qid, party=SELLER, version=1)
        self.assertEqual(detail["status"], "OPEN")
        self.assertEqual(detail["fund_status"][SELLER]["POINTS"]["frozen"], 1000)
        self.assertEqual(detail["fund_status"][SELLER]["POINTS"]["available"], 999_000)

        detail = self.service.sign(qid, party=BUYER, version=1)
        self.assertEqual(detail["status"], "EXECUTED")
        settlement = detail["settlement"]
        self.assertEqual(settlement["status"], "PENDING")
        self.assertEqual(settlement["version"], 1)
        self.assertEqual(settlement["legs"]["POINTS"]["expected"], 1000)
        self.assertEqual(settlement["legs"]["CASH"]["expected"], 20000)
        # 原子性：双方冻结同时转在途，任一腿都不悬空
        self.assertEqual(detail["fund_status"][SELLER]["POINTS"]["frozen"], 0)
        self.assertEqual(detail["fund_status"][SELLER]["POINTS"]["in_transit"], 1000)
        self.assertEqual(detail["fund_status"][BUYER]["CASH"]["frozen"], 0)
        self.assertEqual(detail["fund_status"][BUYER]["CASH"]["in_transit"], 20000)
        # 时间线记录每一步
        self.assertEqual(
            [event["type"] for event in detail["timeline"]],
            ["QUOTE_CREATED", "SIGNED", "SIGNED", "EXECUTED"],
        )

    def test_sign_with_stale_version_rejected(self) -> None:
        qid = self._create()
        with self.assertRaises(VersionMismatch):
            self.service.sign(qid, party=SELLER, version=2)
        self.service.revise(qid, party=BUYER, price=21000)
        with self.assertRaises(VersionMismatch):
            self.service.sign(qid, party=SELLER, version=1)

    def test_sign_by_stranger_rejected(self) -> None:
        qid = self._create()
        with self.assertRaises(NotAParty):
            self.service.sign(qid, party="stranger", version=1)

    def test_duplicate_sign_is_idempotent(self) -> None:
        qid = self._create()
        self.service.sign(qid, party=SELLER, version=1)
        detail = self.service.sign(qid, party=SELLER, version=1)
        self.assertEqual(len(detail["signatures"]), 1)
        freezes = [e for e in detail["ledger"] if e["kind"] == "FREEZE"]
        self.assertEqual(len(freezes), 1)

    # ------------------------------------------------------------------
    # 条款修订 → 签署失效传播 + 补偿分录
    # ------------------------------------------------------------------
    def test_revision_invalidates_prior_signature_and_releases_freeze(self) -> None:
        qid = self._create()
        self.service.sign(qid, party=SELLER, version=1)
        detail = self.service.revise(qid, party=BUYER, price=21000, confirm_seconds=7200, note="调价")

        self.assertEqual(detail["current_version"], 2)
        self.assertEqual(detail["status"], "OPEN")
        signature = detail["signatures"][0]
        self.assertEqual(signature["status"], "INVALIDATED")
        self.assertIn("条款修订", signature["invalidated_reason"])
        # 冻结经补偿分录释放
        self.assertEqual(detail["fund_status"][SELLER]["POINTS"]["frozen"], 0)
        self.assertEqual(detail["fund_status"][SELLER]["POINTS"]["available"], 1_000_000)
        releases = [e for e in detail["ledger"] if e["kind"] == "RELEASE"]
        self.assertEqual(len(releases), 1)
        self.assertTrue(releases[0]["compensating"])

        # 新版本重新双签后成交，清算指令按新条款生成
        detail = self._execute(qid, version=2)
        self.assertEqual(detail["status"], "EXECUTED")
        self.assertEqual(detail["settlement"]["version"], 2)
        self.assertEqual(detail["settlement"]["legs"]["CASH"]["expected"], 21000)

    # ------------------------------------------------------------------
    # 单方撤回
    # ------------------------------------------------------------------
    def test_unilateral_withdraw_releases_counterparty_freeze(self) -> None:
        qid = self._create()
        self.service.sign(qid, party=SELLER, version=1)
        detail = self.service.withdraw(qid, party=BUYER, reason="资金安排变化")

        self.assertEqual(detail["status"], "WITHDRAWN")
        self.assertEqual(detail["signatures"][0]["status"], "INVALIDATED")
        self.assertEqual(detail["fund_status"][SELLER]["POINTS"]["frozen"], 0)
        self.assertEqual(detail["fund_status"][SELLER]["POINTS"]["available"], 1_000_000)
        self.assertTrue(any(e["kind"] == "RELEASE" for e in detail["ledger"]))
        with self.assertRaises(InvalidState):
            self.service.sign(qid, party=BUYER, version=1)

    def test_withdraw_after_execution_rejected(self) -> None:
        qid = self._create()
        self._execute(qid)
        with self.assertRaises(InvalidState):
            self.service.withdraw(qid, party=SELLER)

    # ------------------------------------------------------------------
    # 确认期限超时
    # ------------------------------------------------------------------
    def test_confirmation_deadline_expires_quote_and_releases_freeze(self) -> None:
        qid = self._create(ttl=60)
        self.service.sign(qid, party=SELLER, version=1)
        self.clock.advance(61)

        self.assertEqual(self.service.sweep_expired(), [qid])
        detail = self.service.quote_detail(qid)
        self.assertEqual(detail["status"], "EXPIRED")
        self.assertEqual(detail["signatures"][0]["status"], "INVALIDATED")
        self.assertEqual(detail["fund_status"][SELLER]["POINTS"]["frozen"], 0)
        self.assertTrue(any(e["kind"] == "RELEASE" for e in detail["ledger"]))
        with self.assertRaises(QuoteExpired):
            self.service.sign(qid, party=BUYER, version=1)

    def test_sweep_leaves_open_quotes_untouched(self) -> None:
        qid = self._create(ttl=3600)
        self.clock.advance(60)
        self.assertEqual(self.service.sweep_expired(), [])
        self.assertEqual(self.service.quote_detail(qid)["status"], "OPEN")

    # ------------------------------------------------------------------
    # 部分交割与补偿分录
    # ------------------------------------------------------------------
    def test_partial_delivery_settles_with_compensating_entry(self) -> None:
        qid = self._create()
        self._execute(qid)
        sid = f"S-{qid}"

        result = self.service.settlement_callback(
            sid, callback_id="cb-p1", asset=Asset.POINTS, delivered=600, final=True
        )
        leg = result["settlement"]["legs"]["POINTS"]
        self.assertEqual(leg["status"], "SHORT_FINAL")
        self.assertEqual(leg["delivered"], 600)
        # 买方收到 600 积分，卖方缺口 400 经补偿分录回补
        self.assertEqual(self.service.fund_status(BUYER)["POINTS"]["available"], 600)
        seller = self.service.fund_status(SELLER)
        self.assertEqual(seller["POINTS"]["available"], 999_400)
        self.assertEqual(seller["POINTS"]["in_transit"], 0)

        result = self.service.settlement_callback(
            sid, callback_id="cb-c1", asset=Asset.CASH, delivered=20000, final=True
        )
        self.assertEqual(result["settlement"]["status"], "COMPLETED_WITH_COMPENSATION")
        detail = self.service.quote_detail(qid)
        self.assertEqual(detail["status"], "CLOSED")
        compensations = [e for e in detail["ledger"] if e["kind"] == "COMPENSATE"]
        self.assertEqual(len(compensations), 1)
        self.assertTrue(compensations[0]["compensating"])

    def test_full_delivery_closes_as_settled(self) -> None:
        qid = self._create()
        self._execute(qid)
        sid = f"S-{qid}"
        self.service.settlement_callback(sid, callback_id="cb-p1", asset=Asset.POINTS, delivered=1000, final=True)
        result = self.service.settlement_callback(sid, callback_id="cb-c1", asset=Asset.CASH, delivered=20000, final=True)
        self.assertEqual(result["settlement"]["status"], "SETTLED")
        self.assertEqual(self.service.quote_detail(qid)["status"], "CLOSED")
        # 资金守恒：双方总额不变，仅换手
        self.assertEqual(self.service.fund_status(SELLER)["POINTS"]["available"], 999_000)
        self.assertEqual(self.service.fund_status(SELLER)["CASH"]["available"], 20000)
        self.assertEqual(self.service.fund_status(BUYER)["POINTS"]["available"], 1000)
        self.assertEqual(self.service.fund_status(BUYER)["CASH"]["available"], 480_000)

    def test_incremental_delivery_accumulates(self) -> None:
        qid = self._create()
        self._execute(qid)
        sid = f"S-{qid}"
        self.service.settlement_callback(sid, callback_id="cb-p1", asset=Asset.POINTS, delivered=400)
        result = self.service.settlement_callback(sid, callback_id="cb-p2", asset=Asset.POINTS, delivered=1000, final=True)
        self.assertEqual(result["settlement"]["legs"]["POINTS"]["status"], "FULL")
        self.assertEqual(self.service.fund_status(BUYER)["POINTS"]["available"], 1000)

    # ------------------------------------------------------------------
    # 重复回调幂等
    # ------------------------------------------------------------------
    def test_duplicate_callback_is_deduplicated(self) -> None:
        qid = self._create()
        self._execute(qid)
        sid = f"S-{qid}"
        first = self.service.settlement_callback(
            sid, callback_id="cb-1", asset=Asset.POINTS, delivered=1000, final=True
        )
        self.assertFalse(first["deduplicated"])
        before = self.service.fund_status(BUYER)

        dup = self.service.settlement_callback(
            sid, callback_id="cb-1", asset=Asset.POINTS, delivered=1000, final=True
        )
        self.assertTrue(dup["deduplicated"])
        self.assertEqual(self.service.fund_status(BUYER), before)
        delivers = [
            e for e in self.service.quote_detail(qid)["ledger"] if e["kind"] == "DELIVER"
        ]
        self.assertEqual(len(delivers), 1)
        # 换一个回调号打到已终态的腿 → 冲突
        with self.assertRaises(CallbackConflict):
            self.service.settlement_callback(
                sid, callback_id="cb-2", asset=Asset.POINTS, delivered=1000, final=True
            )

    def test_regressive_or_excessive_callback_rejected(self) -> None:
        qid = self._create()
        self._execute(qid)
        sid = f"S-{qid}"
        self.service.settlement_callback(sid, callback_id="cb-1", asset=Asset.POINTS, delivered=500)
        with self.assertRaises(CallbackConflict):
            self.service.settlement_callback(sid, callback_id="cb-2", asset=Asset.POINTS, delivered=400)
        with self.assertRaises(CallbackConflict):
            self.service.settlement_callback(sid, callback_id="cb-3", asset=Asset.POINTS, delivered=1001)

    # ------------------------------------------------------------------
    # 额度约束
    # ------------------------------------------------------------------
    def test_insufficient_quota_blocks_signing(self) -> None:
        qid = self._create(points=2_000_000)
        with self.assertRaises(InsufficientQuota):
            self.service.sign(qid, party=SELLER, version=1)
        qid = self._create(price=600_000)
        with self.assertRaises(InsufficientQuota):
            self.service.sign(qid, party=BUYER, version=1)


if __name__ == "__main__":
    unittest.main()
