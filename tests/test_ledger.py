"""复式台账的回归测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trade_confirmation.errors import InsufficientQuota, UnbalancedEntry
from trade_confirmation.ledger import Ledger, account_of, system_account
from trade_confirmation.models import Asset, Direction, EntryKind, LedgerEntry, LedgerLine

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_entry(lines, kind=EntryKind.DEPOSIT, entry_id="E-1"):
    return LedgerEntry(
        entry_id=entry_id,
        quote_id=None,
        kind=kind,
        reason="测试",
        lines=tuple(lines),
        created_at=NOW,
        correlation_id="t",
    )


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = Ledger()

    def test_balanced_entry_updates_balances(self) -> None:
        self.ledger.post(
            make_entry(
                [
                    LedgerLine(account_of("alice", Asset.CASH, "available"), Asset.CASH, Direction.DEBIT, 100),
                    LedgerLine(system_account(Asset.CASH), Asset.CASH, Direction.CREDIT, 100),
                ]
            )
        )
        self.assertEqual(self.ledger.balance(account_of("alice", Asset.CASH, "available")), 100)
        self.assertEqual(self.ledger.balance(system_account(Asset.CASH)), -100)

    def test_unbalanced_entry_rejected(self) -> None:
        entry = make_entry(
            [LedgerLine(account_of("alice", Asset.CASH, "available"), Asset.CASH, Direction.DEBIT, 100)]
        )
        with self.assertRaises(UnbalancedEntry):
            self.ledger.post(entry)
        self.assertEqual(self.ledger.entries, [])
        self.assertEqual(self.ledger.balance(account_of("alice", Asset.CASH, "available")), 0)

    def test_overdraft_rejected(self) -> None:
        entry = make_entry(
            [
                LedgerLine(system_account(Asset.POINTS), Asset.POINTS, Direction.DEBIT, 50),
                LedgerLine(account_of("alice", Asset.POINTS, "available"), Asset.POINTS, Direction.CREDIT, 50),
            ]
        )
        with self.assertRaises(InsufficientQuota):
            self.ledger.post(entry)
        self.assertEqual(self.ledger.entries, [])

    def test_non_positive_amount_rejected(self) -> None:
        entry = make_entry(
            [
                LedgerLine(account_of("alice", Asset.CASH, "available"), Asset.CASH, Direction.DEBIT, 0),
                LedgerLine(system_account(Asset.CASH), Asset.CASH, Direction.CREDIT, 0),
            ]
        )
        with self.assertRaises(UnbalancedEntry):
            self.ledger.post(entry)

    def test_entries_for_party_filters_by_prefix(self) -> None:
        self.ledger.post(
            make_entry(
                [
                    LedgerLine(account_of("ab", Asset.CASH, "available"), Asset.CASH, Direction.DEBIT, 10),
                    LedgerLine(system_account(Asset.CASH), Asset.CASH, Direction.CREDIT, 10),
                ]
            )
        )
        self.assertEqual(len(self.ledger.entries_for_party("ab")), 1)
        self.assertEqual(self.ledger.entries_for_party("abc"), [])


if __name__ == "__main__":
    unittest.main()
