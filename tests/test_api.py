"""HTTP 接口冒烟测试：真实起服务、走完整成交链路。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trade_confirmation.api import make_server
from trade_confirmation.service import TradeConfirmationService


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = make_server(TradeConfirmationService(), port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_full_flow_over_http(self) -> None:
        code, _ = self.call("POST", "/accounts/buyer-x/deposit", {"asset": "CASH", "amount": 500_000})
        self.assertEqual(code, 200)
        code, _ = self.call("POST", "/accounts/seller-y/deposit", {"asset": "POINT", "amount": 10_000})
        self.assertEqual(code, 200)

        code, quote = self.call(
            "POST", "/quotes",
            {"buyer": "buyer-x", "seller": "seller-y", "points": 500, "unit_price_cents": 20,
             "confirm_seconds": 3600, "client_ref": "mail-thread-7"},
        )
        self.assertEqual(code, 200)
        qid = quote["quote_id"]
        self.assertEqual(quote["status"], "DRAFT")

        # client_ref 幂等
        code, again = self.call("POST", "/quotes", {"buyer": "buyer-x", "seller": "seller-y", "points": 500,
                                                    "unit_price_cents": 20, "confirm_seconds": 3600,
                                                    "client_ref": "mail-thread-7"})
        self.assertEqual(again["quote_id"], qid)

        code, quote = self.call("POST", f"/quotes/{qid}/publish")
        self.assertEqual(quote["status"], "OPEN")
        self.assertEqual(quote["accounts"]["seller"]["POINT"]["frozen"], 500)
        self.assertEqual(quote["accounts"]["buyer"]["CASH"]["frozen"], 10_000)

        # 修订使旧签署失效
        self.call("POST", f"/quotes/{qid}/sign", {"party": "SELLER"})
        code, quote = self.call("POST", f"/quotes/{qid}/revise",
                                {"points": 600, "unit_price_cents": 20, "confirm_seconds": 3600})
        self.assertEqual(code, 200)
        self.assertEqual(quote["versions"][0]["signatures"][0]["status"], "INVALIDATED")
        self.assertEqual(quote["current_version"], 2)

        # 同版双签 → 原子成交
        self.call("POST", f"/quotes/{qid}/sign", {"party": "BUYER"})
        code, quote = self.call("POST", f"/quotes/{qid}/sign", {"party": "SELLER"})
        self.assertEqual(quote["status"], "EXECUTED")
        iid = quote["clearing"][0]

        # 部分交割 + 重复回调幂等
        code, result = self.call("POST", f"/clearing/{iid}/deliver",
                                 {"callback_id": "cb-1", "points": 300, "cash": 6_000})
        self.assertEqual(result["status"], "PARTIAL")
        code, dup = self.call("POST", f"/clearing/{iid}/deliver",
                              {"callback_id": "cb-1", "points": 300, "cash": 6_000})
        self.assertTrue(dup["duplicate"])

        # 关闭清算，余量补偿
        code, clearing = self.call("POST", f"/clearing/{iid}/close")
        self.assertEqual(clearing["status"], "COMPENSATED")
        self.assertEqual(clearing["escrow"]["POINT"]["available"], 0)

        # 接口展示每一步签署与资金状态
        code, view = self.call("GET", f"/quotes/{qid}")
        kinds = [e["kind"] for e in view["timeline"]]
        for expected in ("QUOTE_CREATED", "PUBLISHED", "SIGNED", "SIGNATURE_INVALIDATED",
                         "REVISED", "EXECUTED", "DELIVERED", "DUPLICATE_CALLBACK", "CLEARING_CLOSED"):
            self.assertIn(expected, kinds)
        code, account = self.call("GET", "/accounts/buyer-x")
        self.assertEqual(account["balances"]["POINT"]["available"], 300)
        self.assertEqual(account["balances"]["CASH"]["available"], 500_000 - 6_000)

    def test_error_mapping(self) -> None:
        code, body = self.call("GET", "/quotes/Q-9999")
        self.assertEqual(code, 404)
        code, body = self.call("POST", "/quotes", {"buyer": "a"})  # 缺字段
        self.assertEqual(code, 400)
        code, body = self.call("GET", "/no-such-route")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
