"""HTTP 接口回归测试：每一步签署与资金状态可见。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trade_confirmation.api import create_server
from trade_confirmation.service import TradeConfirmationService


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = TradeConfirmationService()
        self.server = create_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, method: str, path: str, payload: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def _get(self, path: str):
        return self._request("GET", path)

    def _post(self, path: str, payload: dict):
        return self._request("POST", path, payload)

    def _seed_quote(self) -> str:
        status, _ = self._post("/accounts/sellerA/deposit", {"asset": "POINTS", "amount": 100_000})
        self.assertEqual(status, 200)
        status, _ = self._post("/accounts/buyerB/deposit", {"asset": "CASH", "amount": 50_000})
        self.assertEqual(status, 200)
        status, quote = self._post(
            "/quotes",
            {"seller": "sellerA", "buyer": "buyerB", "points": 1000, "price": 20000, "confirm_seconds": 3600},
        )
        self.assertEqual(status, 201)
        return quote["quote_id"]

    def test_full_bilateral_flow_visible_via_api(self) -> None:
        qid = self._seed_quote()

        # 卖方签署 v1：冻结在资金状态中可见
        status, detail = self._post(f"/quotes/{qid}/sign", {"party": "sellerA", "version": 1})
        self.assertEqual(status, 200)
        self.assertEqual(detail["fund_status"]["sellerA"]["POINTS"]["frozen"], 1000)
        self.assertEqual([s["status"] for s in detail["signatures"]], ["ACTIVE"])

        # 买方签署 v1：同版双签，原子成交
        status, detail = self._post(f"/quotes/{qid}/sign", {"party": "buyerB", "version": 1})
        self.assertEqual(detail["status"], "EXECUTED")
        self.assertEqual(detail["fund_status"]["sellerA"]["POINTS"]["in_transit"], 1000)
        self.assertEqual(detail["fund_status"]["buyerB"]["CASH"]["in_transit"], 20000)
        sid = detail["settlement"]["settlement_id"]

        # 清算回调：积分腿全额，资金腿部分交割后终态 → 补偿分录
        status, result = self._post(
            f"/settlements/{sid}/callbacks",
            {"callback_id": "cb-1", "asset": "POINTS", "delivered": 1000, "final": True},
        )
        self.assertEqual(status, 200)
        status, result = self._post(
            f"/settlements/{sid}/callbacks",
            {"callback_id": "cb-2", "asset": "CASH", "delivered": 15000, "final": True},
        )
        self.assertEqual(result["settlement"]["status"], "COMPLETED_WITH_COMPENSATION")

        # 重复回调幂等去重
        status, dup = self._post(
            f"/settlements/{sid}/callbacks",
            {"callback_id": "cb-2", "asset": "CASH", "delivered": 15000, "final": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(dup["deduplicated"])

        # 报价详情：状态、签署、分录、时间线、双方资金状态齐全
        status, detail = self._get(f"/quotes/{qid}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["status"], "CLOSED")
        kinds = [entry["kind"] for entry in detail["ledger"]]
        self.assertEqual(
            kinds,
            ["FREEZE", "FREEZE", "SETTLE", "DELIVER", "DELIVER", "COMPENSATE"],
        )
        self.assertEqual(detail["fund_status"]["buyerB"]["CASH"]["available"], 35_000)
        self.assertEqual(detail["fund_status"]["sellerA"]["CASH"]["available"], 15_000)
        event_types = [event["type"] for event in detail["timeline"]]
        self.assertIn("COMPENSATION_POSTED", event_types)

        # 账户维度同样可查
        status, account = self._get("/accounts/sellerA")
        self.assertEqual(status, 200)
        self.assertEqual(account["fund_status"]["POINTS"]["available"], 99_000)

    def test_revision_and_withdraw_via_api(self) -> None:
        qid = self._seed_quote()
        self._post(f"/quotes/{qid}/sign", {"party": "sellerA", "version": 1})

        # 条款修订：旧签署失效，冻结释放
        status, detail = self._post(
            f"/quotes/{qid}/revise", {"party": "buyerB", "price": 21000, "confirm_seconds": 7200}
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["current_version"], 2)
        self.assertEqual(detail["signatures"][0]["status"], "INVALIDATED")
        self.assertEqual(detail["fund_status"]["sellerA"]["POINTS"]["frozen"], 0)

        # 旧版本签署被拒绝
        status, error = self._post(f"/quotes/{qid}/sign", {"party": "buyerB", "version": 1})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "VERSION_MISMATCH")

        # 单方撤回
        status, detail = self._post(f"/quotes/{qid}/withdraw", {"party": "sellerA"})
        self.assertEqual(status, 200)
        self.assertEqual(detail["status"], "WITHDRAWN")

    def test_error_mapping(self) -> None:
        status, error = self._get("/quotes/Q-404")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "NOT_FOUND")

        qid = self._seed_quote()
        status, error = self._post(f"/quotes/{qid}/sign", {"party": "stranger", "version": 1})
        self.assertEqual(status, 403)
        self.assertEqual(error["error"]["code"], "NOT_A_PARTY")

        status, error = self._post(f"/quotes/{qid}/sign", {"party": "sellerA", "version": 9})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "VERSION_MISMATCH")

        status, error = self._post("/quotes", {"seller": "s", "buyer": "b"})
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "INVALID_INPUT")

    def test_list_and_maintenance_endpoints(self) -> None:
        qid = self._seed_quote()
        status, listing = self._get("/quotes")
        self.assertEqual(status, 200)
        self.assertEqual([q["quote_id"] for q in listing["quotes"]], [qid])

        status, result = self._post("/maintenance/sweep", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["expired"], [])

        status, health = self._get("/health")
        self.assertEqual((status, health["status"]), (200, "ok"))


if __name__ == "__main__":
    unittest.main()
