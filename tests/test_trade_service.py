"""双边成交确认核心规则测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trade_confirmation.errors import DomainError, InsufficientQuotaError, StateError
from trade_confirmation.models import Asset, Party
from trade_confirmation.service import TradeConfirmationService


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.svc = TradeConfirmationService(clock=self.clock)
        self.svc.deposit("buyer-a", Asset.CASH, 1_000_000)  # 10000.00 元
        self.svc.deposit("seller-b", Asset.POINT, 100_000)

    def balance(self, owner: str, asset: Asset) -> tuple[int, int]:
        view = self.svc.get_account(owner)["balances"][asset.value]
        return view["available"], view["frozen"]

    def open_quote(self, points: int = 1_000, price: int = 50, confirm: float = 3600) -> str:
        view = self.svc.create_quote("buyer-a", "seller-b", points, price, confirm)
        self.svc.publish(view["quote_id"])
        return view["quote_id"]


class DualSignExecutionTest(ServiceTestBase):
    def test_dual_sign_same_version_executes_atomically(self) -> None:
        qid = self.open_quote()
        # 发布后双向冻结
        self.assertEqual(self.balance("seller-b", Asset.POINT), (99_000, 1_000))
        self.assertEqual(self.balance("buyer-a", Asset.CASH), (950_000, 50_000))

        self.svc.sign(qid, "SELLER")
        mid = self.svc.get_quote(qid)
        self.assertEqual(mid["status"], "OPEN")  # 单签不成交

        view = self.svc.sign(qid, "BUYER")
        self.assertEqual(view["status"], "EXECUTED")
        self.assertEqual(view["versions"][0]["status"], "EXECUTED")
        sigs = view["versions"][0]["signatures"]
        self.assertEqual({s["party"] for s in sigs}, {"BUYER", "SELLER"})
        self.assertTrue(all(s["status"] == "CONSUMED" for s in sigs))
        # 冻结被消耗，转入清算托管户
        self.assertEqual(self.balance("seller-b", Asset.POINT), (99_000, 0))
        self.assertEqual(self.balance("buyer-a", Asset.CASH), (950_000, 0))
        self.assertEqual(len(view["clearing"]), 1)
        clearing = self.svc.get_clearing(view["clearing"][0])
        self.assertEqual(clearing["status"], "PENDING")
        self.assertEqual(clearing["escrow"]["POINT"]["available"], 1_000)
        self.assertEqual(clearing["escrow"]["CASH"]["available"], 50_000)
        self.svc.ledger.verify()

    def test_sign_is_idempotent_per_party(self) -> None:
        qid = self.open_quote()
        self.svc.sign(qid, "SELLER")
        view = self.svc.sign(qid, "SELLER")  # 重复签署不产生新记录
        sigs = view["versions"][0]["signatures"]
        self.assertEqual(len(sigs), 1)
        self.assertEqual(view["status"], "OPEN")

    def test_create_quote_idempotent_by_client_ref(self) -> None:
        first = self.svc.create_quote("buyer-a", "seller-b", 100, 10, 60, client_ref="mail-001")
        second = self.svc.create_quote("buyer-a", "seller-b", 100, 10, 60, client_ref="mail-001")
        self.assertEqual(first["quote_id"], second["quote_id"])


class RevisionTest(ServiceTestBase):
    def test_revision_invalidates_old_signature_and_releases_freeze(self) -> None:
        qid = self.open_quote()
        self.svc.sign(qid, "SELLER")
        view = self.svc.revise(qid, 2_000, 60, 7_200)

        old, new = view["versions"]
        self.assertEqual(old["status"], "SUPERSEDED")
        self.assertEqual(old["signatures"][0]["status"], "INVALIDATED")
        self.assertEqual(new["version_no"], 2)
        self.assertEqual(new["status"], "ACTIVE")
        # 旧冻结释放、新冻结生效（2000 积分 / 120000 分）
        self.assertEqual(self.balance("seller-b", Asset.POINT), (98_000, 2_000))
        self.assertEqual(self.balance("buyer-a", Asset.CASH), (880_000, 120_000))
        # 释放分录引用了原冻结分录
        releases = [e for e in view["ledger"] if e["entry_type"] == "RELEASE"]
        self.assertEqual(len(releases), 2)
        self.assertTrue(all(e["compensates"] for e in releases))
        kinds = [e["kind"] for e in view["timeline"]]
        self.assertIn("SIGNATURE_INVALIDATED", kinds)
        self.svc.ledger.verify()

    def test_cross_version_dual_sign_cannot_execute(self) -> None:
        qid = self.open_quote()
        self.svc.sign(qid, "SELLER")  # 卖方签的是 v1
        self.svc.revise(qid, 500, 40, 3_600)
        view = self.svc.sign(qid, "BUYER")  # 买方签的是 v2
        self.assertEqual(view["status"], "OPEN")  # 不同版本不算双签
        view = self.svc.sign(qid, "SELLER")  # 卖方重签 v2 才成交
        self.assertEqual(view["status"], "EXECUTED")
        clearing = self.svc.get_clearing(view["clearing"][0])
        self.assertEqual(clearing["points"], 500)
        self.assertEqual(clearing["total_cents"], 20_000)
        self.svc.ledger.verify()

    def test_revision_rejected_when_new_terms_exceed_quota(self) -> None:
        qid = self.open_quote()
        with self.assertRaises(InsufficientQuotaError):
            self.svc.revise(qid, 500_000, 50, 3_600)  # 卖方积分不够
        view = self.svc.get_quote(qid)
        self.assertEqual(view["status"], "OPEN")  # 原版本不受影响
        self.assertEqual(self.balance("seller-b", Asset.POINT), (99_000, 1_000))
        self.svc.ledger.verify()


class WithdrawAndExpireTest(ServiceTestBase):
    def test_unilateral_withdraw_releases_both_sides(self) -> None:
        qid = self.open_quote()
        self.svc.sign(qid, "BUYER")
        view = self.svc.withdraw(qid, "SELLER")
        self.assertEqual(view["status"], "WITHDRAWN")
        self.assertEqual(view["versions"][0]["status"], "VOID")
        self.assertEqual(view["versions"][0]["signatures"][0]["status"], "INVALIDATED")
        # 双方额度在同一动作内全部释放，不存在只放一方
        self.assertEqual(self.balance("seller-b", Asset.POINT), (100_000, 0))
        self.assertEqual(self.balance("buyer-a", Asset.CASH), (1_000_000, 0))
        releases = [e for e in view["ledger"] if e["entry_type"] == "RELEASE"]
        self.assertEqual(len(releases), 2)
        self.assertEqual({e["owner"] for e in releases}, {"buyer-a", "seller-b"})
        self.svc.ledger.verify()

    def test_withdraw_after_execution_rejected(self) -> None:
        qid = self.open_quote()
        self.svc.sign(qid, "SELLER")
        self.svc.sign(qid, "BUYER")
        with self.assertRaises(StateError):
            self.svc.withdraw(qid, "SELLER")

    def test_timeout_expires_and_releases_freeze(self) -> None:
        qid = self.open_quote(confirm=600)
        self.svc.sign(qid, "SELLER")
        self.clock.advance(601)
        self.assertEqual(self.svc.sweep_expired(), [qid])
        view = self.svc.get_quote(qid)
        self.assertEqual(view["status"], "EXPIRED")
        self.assertEqual(view["versions"][0]["signatures"][0]["status"], "INVALIDATED")
        self.assertEqual(self.balance("seller-b", Asset.POINT), (100_000, 0))
        self.assertEqual(self.balance("buyer-a", Asset.CASH), (1_000_000, 0))
        # 超时后不能再签署
        with self.assertRaises(StateError):
            self.svc.sign(qid, "BUYER")
        self.svc.ledger.verify()

    def test_lazy_expiry_on_access(self) -> None:
        qid = self.open_quote(confirm=60)
        self.clock.advance(61)
        view = self.svc.get_quote(qid)  # 读取即触发超时判定
        self.assertEqual(view["status"], "EXPIRED")

    def test_sign_before_deadline_still_works(self) -> None:
        qid = self.open_quote(confirm=600)
        self.clock.advance(599)
        self.svc.sign(qid, "SELLER")
        view = self.svc.sign(qid, "BUYER")
        self.assertEqual(view["status"], "EXECUTED")


class ClearingTest(ServiceTestBase):
    def execute(self, points: int = 1_000, price: int = 50) -> tuple[str, str]:
        qid = self.open_quote(points, price)
        self.svc.sign(qid, "SELLER")
        view = self.svc.sign(qid, "BUYER")
        return qid, view["clearing"][0]

    def test_full_delivery_completes_clearing(self) -> None:
        _, iid = self.execute()
        result = self.svc.deliver(iid, "cb-1", 1_000, 50_000)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.balance("buyer-a", Asset.POINT), (1_000, 0))
        self.assertEqual(self.balance("seller-b", Asset.CASH), (50_000, 0))
        clearing = self.svc.get_clearing(iid)
        self.assertEqual(clearing["escrow"]["POINT"]["available"], 0)
        self.assertEqual(clearing["escrow"]["CASH"]["available"], 0)
        self.svc.ledger.verify()

    def test_partial_delivery_then_close_compensates_remainder(self) -> None:
        _, iid = self.execute()
        self.svc.deliver(iid, "cb-1", 400, 20_000)  # 部分交割
        clearing = self.svc.get_clearing(iid)
        self.assertEqual(clearing["status"], "PARTIAL")
        self.assertEqual(clearing["remaining_points"], 600)
        self.assertEqual(clearing["remaining_cents"], 30_000)

        self.svc.close_clearing(iid)
        clearing = self.svc.get_clearing(iid)
        self.assertEqual(clearing["status"], "COMPENSATED")
        # 已交割部分归对方，余量补偿退回原主
        self.assertEqual(self.balance("buyer-a", Asset.POINT), (400, 0))
        self.assertEqual(self.balance("seller-b", Asset.POINT), (99_600, 0))
        self.assertEqual(self.balance("seller-b", Asset.CASH), (20_000, 0))
        self.assertEqual(self.balance("buyer-a", Asset.CASH), (980_000, 0))
        comp = [e for e in clearing["entries"] if e["entry_type"] == "COMPENSATE"]
        self.assertEqual(len(comp), 2)
        self.assertTrue(all(e["compensates"] for e in comp))
        self.svc.ledger.verify()

    def test_duplicate_callback_is_idempotent(self) -> None:
        _, iid = self.execute()
        first = self.svc.deliver(iid, "cb-1", 400, 20_000)
        entry_count = len(self.svc.ledger.entries)
        second = self.svc.deliver(iid, "cb-1", 400, 20_000)  # 重复回调
        self.assertTrue(second["duplicate"])
        self.assertFalse(first["duplicate"])
        self.assertEqual(second["delivered_points"], first["delivered_points"])
        self.assertEqual(len(self.svc.ledger.entries), entry_count)  # 不产生新分录
        self.assertEqual(self.balance("buyer-a", Asset.POINT), (400, 0))
        # 不同 callback_id 继续交割剩余部分
        third = self.svc.deliver(iid, "cb-2", 600, 30_000)
        self.assertEqual(third["status"], "COMPLETED")
        self.svc.ledger.verify()

    def test_delivery_beyond_remaining_rejected(self) -> None:
        _, iid = self.execute()
        with self.assertRaises(DomainError):
            self.svc.deliver(iid, "cb-1", 1_001, 50_000)
        self.svc.deliver(iid, "cb-2", 1_000, 50_000)
        with self.assertRaises(StateError):  # 已完成不可再交割
            self.svc.deliver(iid, "cb-3", 1, 1)

    def test_double_close_rejected(self) -> None:
        _, iid = self.execute()
        self.svc.close_clearing(iid)
        with self.assertRaises(StateError):
            self.svc.close_clearing(iid)


class QuotaGuardTest(ServiceTestBase):
    def test_publish_fails_when_quota_insufficient(self) -> None:
        view = self.svc.create_quote("buyer-a", "seller-b", 200_000, 50, 3_600)
        with self.assertRaises(InsufficientQuotaError):
            self.svc.publish(view["quote_id"])
        self.assertEqual(self.balance("seller-b", Asset.POINT), (100_000, 0))
        self.assertEqual(self.balance("buyer-a", Asset.CASH), (1_000_000, 0))
        self.svc.ledger.verify()

    def test_buyer_cash_checked_at_publish(self) -> None:
        view = self.svc.create_quote("buyer-a", "seller-b", 1_000, 5_000, 3_600)  # 需 50000.00 元
        with self.assertRaises(InsufficientQuotaError):
            self.svc.publish(view["quote_id"])
        self.svc.ledger.verify()


class ViewTest(ServiceTestBase):
    def test_view_shows_every_signing_and_fund_step(self) -> None:
        qid = self.open_quote()
        self.svc.sign(qid, "SELLER")
        view = self.svc.sign(qid, "BUYER")
        kinds = [e["kind"] for e in view["timeline"]]
        self.assertEqual(
            kinds,
            ["QUOTE_CREATED", "PUBLISHED", "SIGNED", "SIGNED", "EXECUTED"],
        )
        entry_types = [e["entry_type"] for e in view["ledger"]]
        self.assertEqual(entry_types.count("FREEZE"), 2)
        self.assertEqual(entry_types.count("SETTLE"), 4)  # 双方出冻结 + 托管入账
        self.assertEqual(len(view["freezes"]), 2)
        self.assertTrue(all(f["status"] == "CONSUMED" for f in view["freezes"]))
        # 每一步账户余额都体现在视图里
        self.assertIn("buyer", view["accounts"])
        self.assertIn("seller", view["accounts"])


if __name__ == "__main__":
    unittest.main()
