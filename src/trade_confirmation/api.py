"""基于标准库的 HTTP 接口：展示每一步签署与资金状态。

路由一览：
    POST /accounts/{owner}/deposit      注资 {asset, amount}
    GET  /accounts/{owner}              余额与分录
    POST /quotes                        创建报价 {buyer, seller, points, unit_price_cents, confirm_seconds, client_ref?}
    GET  /quotes/{qid}                  报价全量视图（版本/签署/冻结/分录/时间线）
    POST /quotes/{qid}/publish          发布当前版本（双向冻结）
    POST /quotes/{qid}/revise           修订条款 {points, unit_price_cents, confirm_seconds}
    POST /quotes/{qid}/sign             签署 {party: BUYER|SELLER}
    POST /quotes/{qid}/withdraw         单方撤回 {party}
    POST /quotes/{qid}/expire           触发单笔超时检查
    POST /sweep                         批量超时扫描
    GET  /clearing/{iid}                清算指令状态
    POST /clearing/{iid}/deliver        交割回调 {callback_id, points, cash}（幂等）
    POST /clearing/{iid}/close          关闭清算，余量补偿退回
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .errors import DomainError, InsufficientQuotaError, NotFoundError, StateError
from .service import TradeConfirmationService

HandlerFn = Callable[["_Handler", re.Match, dict], object]

_ROUTES: list[tuple[str, re.Pattern, str]] = [
    ("POST", re.compile(r"^/accounts/(?P<owner>[^/]+)/deposit$"), "deposit"),
    ("GET", re.compile(r"^/accounts/(?P<owner>[^/]+)$"), "account"),
    ("POST", re.compile(r"^/quotes$"), "create_quote"),
    ("GET", re.compile(r"^/quotes/(?P<qid>[^/]+)$"), "get_quote"),
    ("POST", re.compile(r"^/quotes/(?P<qid>[^/]+)/publish$"), "publish"),
    ("POST", re.compile(r"^/quotes/(?P<qid>[^/]+)/revise$"), "revise"),
    ("POST", re.compile(r"^/quotes/(?P<qid>[^/]+)/sign$"), "sign"),
    ("POST", re.compile(r"^/quotes/(?P<qid>[^/]+)/withdraw$"), "withdraw"),
    ("POST", re.compile(r"^/quotes/(?P<qid>[^/]+)/expire$"), "expire"),
    ("POST", re.compile(r"^/sweep$"), "sweep"),
    ("GET", re.compile(r"^/clearing/(?P<iid>[^/]+)$"), "get_clearing"),
    ("POST", re.compile(r"^/clearing/(?P<iid>[^/]+)/deliver$"), "deliver"),
    ("POST", re.compile(r"^/clearing/(?P<iid>[^/]+)/close$"), "close_clearing"),
]


def _require(body: dict, *keys: str) -> list:
    missing = [k for k in keys if k not in body]
    if missing:
        raise DomainError("缺少必填字段：" + "、".join(missing))
    return [body[k] for k in keys]


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    # ---- 各路由处理 ----

    def deposit(self, m: re.Match, body: dict) -> object:
        asset, amount = _require(body, "asset", "amount")
        return self.server.service.deposit(m.group("owner"), asset, amount)

    def account(self, m: re.Match, body: dict) -> object:
        return self.server.service.get_account(m.group("owner"))

    def create_quote(self, m: re.Match, body: dict) -> object:
        buyer, seller, points, price, confirm = _require(
            body, "buyer", "seller", "points", "unit_price_cents", "confirm_seconds"
        )
        return self.server.service.create_quote(
            buyer=buyer,
            seller=seller,
            points=points,
            unit_price_cents=price,
            confirm_seconds=confirm,
            client_ref=body.get("client_ref"),
        )

    def get_quote(self, m: re.Match, body: dict) -> object:
        return self.server.service.get_quote(m.group("qid"))

    def publish(self, m: re.Match, body: dict) -> object:
        return self.server.service.publish(m.group("qid"))

    def revise(self, m: re.Match, body: dict) -> object:
        points, price, confirm = _require(body, "points", "unit_price_cents", "confirm_seconds")
        return self.server.service.revise(m.group("qid"), points, price, confirm)

    def sign(self, m: re.Match, body: dict) -> object:
        (party,) = _require(body, "party")
        return self.server.service.sign(m.group("qid"), party)

    def withdraw(self, m: re.Match, body: dict) -> object:
        (party,) = _require(body, "party")
        return self.server.service.withdraw(m.group("qid"), party)

    def expire(self, m: re.Match, body: dict) -> object:
        return self.server.service.expire(m.group("qid"))

    def sweep(self, m: re.Match, body: dict) -> object:
        return {"expired": self.server.service.sweep_expired()}

    def get_clearing(self, m: re.Match, body: dict) -> object:
        return self.server.service.get_clearing(m.group("iid"))

    def deliver(self, m: re.Match, body: dict) -> object:
        callback_id, points, cash = _require(body, "callback_id", "points", "cash")
        return self.server.service.deliver(m.group("iid"), callback_id, points, cash)

    def close_clearing(self, m: re.Match, body: dict) -> object:
        return self.server.service.close_clearing(m.group("iid"))

    # ---- 框架 ----

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            for route_method, pattern, action in _ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(self.path.split("?", 1)[0])
                if match:
                    body = self._read_body() if method == "POST" else {}
                    result = getattr(self, action)(match, body)
                    self._send(200, result)
                    return
            self._send(404, {"error": f"路由不存在：{method} {self.path}"})
        except NotFoundError as exc:
            self._send(404, {"error": str(exc)})
        except (StateError, InsufficientQuotaError) as exc:
            self._send(409, {"error": str(exc)})
        except (DomainError, ValueError, KeyError) as exc:
            self._send(400, {"error": str(exc)})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}") from None
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    def _send(self, code: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # 静默访问日志
        pass


class _Server(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], service: TradeConfirmationService):
        super().__init__(address, _Handler)
        self.service = service


def make_server(service: TradeConfirmationService, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """构建 HTTP 服务（port=0 时由系统分配端口，便于测试）。"""
    return _Server((host, port), service)
