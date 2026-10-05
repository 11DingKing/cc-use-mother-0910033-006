"""启动 HTTP 服务：python3 -m trade_confirmation [--host H] [--port N] [--demo]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_confirmation.api import make_server
from trade_confirmation.models import Asset
from trade_confirmation.service import TradeConfirmationService


def main() -> None:
    parser = argparse.ArgumentParser(description="双边成交确认撤销后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true", help="注入演示账户额度")
    args = parser.parse_args()

    service = TradeConfirmationService()
    if args.demo:
        service.deposit("buyer-a", Asset.CASH, 10_000_000)
        service.deposit("seller-b", Asset.POINT, 500_000)
        print("演示账户：buyer-a（资金 100000.00 元）、seller-b（积分 500000）")

    server = make_server(service, args.host, args.port)
    host, port = server.server_address[:2]
    print(f"双边成交确认服务已启动：http://{host}:{port}")
    print("示例：curl -X POST %s/quotes -d '{\"buyer\":\"buyer-a\",\"seller\":\"seller-b\",\"points\":1000,\"unit_price_cents\":50,\"confirm_seconds\":3600}'" % f"http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
