"""标准库 HTTP 接口：展示每一步签署与资金状态。"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .errors import DomainError, InvalidInput, NotFound
from .models import Asset
from .service import TradeConfirmationService


def _require_str(body: dict, field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"缺少必填字段：{field}")
    return value.strip()


def _parse_asset(value: object) -> Asset:
    try:
        return Asset(value)
    except ValueError:
        raise InvalidInput(f"未知资产类型：{value!r}，应为 POINTS 或 CASH") from None


# ----------------------------------------------------------------------
# 路由处理：每个函数返回 (响应体, HTTP 状态)
# ----------------------------------------------------------------------
def _health(service: TradeConfirmationService, handler: BaseHTTPRequestHandler):
    return {"status": "ok"}, 200


def _create_quote(service, handler):
    body = handler.read_json()
    return service.create_quote(
        seller=_require_str(body, "seller"),
        buyer=_require_str(body, "buyer"),
        points=body.get("points"),
        price=body.get("price"),
        confirm_seconds=body.get("confirm_seconds"),
        note=body.get("note", ""),
        quote_id=body.get("quote_id"),
    ), 201


def _list_quotes(service, handler):
    return {"quotes": service.list_quotes()}, 200


def _get_quote(service, handler, quote_id):
    return service.quote_detail(quote_id), 200


def _sign(service, handler, quote_id):
    body = handler.read_json()
    return service.sign(
        quote_id, party=_require_str(body, "party"), version=body.get("version")
    ), 200


def _revise(service, handler, quote_id):
    body = handler.read_json()
    return service.revise(
        quote_id,
        party=_require_str(body, "party"),
        points=body.get("points"),
        price=body.get("price"),
        confirm_seconds=body.get("confirm_seconds"),
        note=body.get("note", ""),
    ), 200


def _withdraw(service, handler, quote_id):
    body = handler.read_json()
    return service.withdraw(
        quote_id, party=_require_str(body, "party"), reason=body.get("reason", "")
    ), 200


def _expire(service, handler, quote_id):
    return service.expire(quote_id), 200


def _sweep(service, handler):
    return {"expired": service.sweep_expired()}, 200


def _callback(service, handler, settlement_id):
    body = handler.read_json()
    return service.settlement_callback(
        settlement_id,
        callback_id=_require_str(body, "callback_id"),
        asset=_parse_asset(body.get("asset")),
        delivered=body.get("delivered"),
        final=bool(body.get("final", False)),
    ), 200


def _get_settlement(service, handler, settlement_id):
    return service.settlement_detail(settlement_id), 200


def _get_account(service, handler, party):
    return service.account_detail(party), 200


def _deposit(service, handler, party):
    body = handler.read_json()
    return service.deposit(
        party, _parse_asset(body.get("asset")), body.get("amount")
    ), 200


ROUTES = [
    ("GET", re.compile(r"^/health$"), _health),
    ("POST", re.compile(r"^/quotes$"), _create_quote),
    ("GET", re.compile(r"^/quotes$"), _list_quotes),
    ("GET", re.compile(r"^/quotes/(?P<quote_id>[^/]+)$"), _get_quote),
    ("POST", re.compile(r"^/quotes/(?P<quote_id>[^/]+)/sign$"), _sign),
    ("POST", re.compile(r"^/quotes/(?P<quote_id>[^/]+)/revise$"), _revise),
    ("POST", re.compile(r"^/quotes/(?P<quote_id>[^/]+)/withdraw$"), _withdraw),
    ("POST", re.compile(r"^/quotes/(?P<quote_id>[^/]+)/expire$"), _expire),
    ("POST", re.compile(r"^/maintenance/sweep$"), _sweep),
    ("POST", re.compile(r"^/settlements/(?P<settlement_id>[^/]+)/callbacks$"), _callback),
    ("GET", re.compile(r"^/settlements/(?P<settlement_id>[^/]+)$"), _get_settlement),
    ("GET", re.compile(r"^/accounts/(?P<party>[^/]+)$"), _get_account),
    ("POST", re.compile(r"^/accounts/(?P<party>[^/]+)/deposit$"), _deposit),
]


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def make_handler(service: TradeConfirmationService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TradeConfirmation/0.1"

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise InvalidInput(f"请求体不是合法 JSON：{exc}") from None
            if not isinstance(data, dict):
                raise InvalidInput("请求体必须是 JSON 对象")
            return data

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                path = self.path.split("?", 1)[0]
                for route_method, pattern, func in ROUTES:
                    if route_method != method:
                        continue
                    match = pattern.match(path)
                    if match:
                        groups = {key: unquote(value) for key, value in match.groupdict().items()}
                        payload, status = func(service, self, **groups)
                        self._send(status, payload)
                        return
                raise NotFound(f"路由不存在：{method} {path}")
            except DomainError as exc:
                self._send(exc.http_status, {"error": {"code": exc.code, "message": exc.message}})
            except Exception as exc:  # pragma: no cover - 兜底
                self._send(500, {"error": {"code": "INTERNAL", "message": str(exc)}})

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # 静默访问日志
            pass

    return Handler


def create_server(service: TradeConfirmationService, host: str, port: int) -> _Server:
    return _Server((host, port), make_handler(service))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="双边成交确认撤销后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    server = create_server(TradeConfirmationService(), args.host, args.port)
    print(f"双边成交确认撤销后端已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
