"""Minimal, dependency-free JSON-RPC 2.0 dispatcher for the gateway.

Supports request/response as well as server-side notifications (for push streams
such as live logs). Methods are registered as ``async def handler(gateway, ctx, params)``.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

log = logging.getLogger("red.pdc.pdc_webdashboard.rpc")

# Standard JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# application-specific
UNAUTHORIZED = -32000
FORBIDDEN = -32001

Handler = Callable[..., Awaitable[Any]]


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class Dispatcher:
    def __init__(self) -> None:
        self._methods: Dict[str, Handler] = {}

    def method(self, name: str) -> Callable[[Handler], Handler]:
        def deco(func: Handler) -> Handler:
            self._methods[name] = func
            return func
        return deco

    def register(self, name: str, func: Handler) -> None:
        self._methods[name] = func

    async def dispatch(self, gateway: Any, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Processes a single JSON-RPC message and returns the response."""
        req_id = message.get("id")
        is_notification = "id" not in message

        if message.get("jsonrpc") != "2.0" or "method" not in message:
            return _error(req_id, INVALID_REQUEST, "Ungültige JSON-RPC-2.0-Anfrage")

        method_name = message["method"]
        params = message.get("params") or {}
        handler = self._methods.get(method_name)
        if handler is None:
            return _error(req_id, METHOD_NOT_FOUND, f"Methode '{method_name}' unbekannt")

        try:
            result = await handler(gateway, params)
        except RpcError as e:
            log.debug("RPC-Fehler bei %s: %s", method_name, e.message)
            return _error(req_id, e.code, e.message, e.data)
        except Exception as e:  # pragma: no cover - defensive
            log.exception("Interner Fehler bei Methode %s", method_name)
            return _error(req_id, INTERNAL_ERROR, str(e))

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}
