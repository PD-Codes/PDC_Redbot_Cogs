"""aiohttp-based RPC gateway.

- WebSocket ``/rpc``  : JSON-RPC 2.0 (request/response + server push for streams)
- REST ``/api/health``: liveness without auth
- REST ``/api/manifest``: convenient GET mirror of ``manifest.get``

Auth between BFF and gateway via a shared secret (constant-time comparison).
Default binding: 127.0.0.1 (localhost only).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, Optional, Set

from aiohttp import WSMsgType, web

from .methods import dispatcher
from .rpc import UNAUTHORIZED

log = logging.getLogger("red.pdc.pdc_webdashboard.gateway")

# How many request-log entries are kept in memory (ring buffer).
REQUEST_LOG_MAX = 500


class Gateway:
    def __init__(self, bot: Any, registry: Any, *, token: str, host: str = "127.0.0.1",
                 port: int = 6970, audit_sink=None,
                 cors_origins: Optional[Iterable[str]] = None,
                 request_log: bool = False) -> None:
        self.bot = bot
        self.registry = registry
        self.token = token
        self.host = host
        self.port = port
        self._audit_sink = audit_sink
        # CORS: exact-match allow-list of browser origins (empty = no CORS headers).
        self.cors_origins: Set[str] = set(cors_origins or [])
        # Optional request logging for auditing (in-memory ring buffer + log lines).
        self.request_log_enabled: bool = bool(request_log)
        self.request_log: Deque[Dict[str, Any]] = deque(maxlen=REQUEST_LOG_MAX)

        self.app = web.Application(middlewares=[self._auth_middleware])
        self.app.add_routes([
            web.get("/api/health", self._health),
            web.get("/api/manifest", self._manifest_rest),
            web.post("/rpc", self._rpc_post),   # request/response (BFF)
            web.get("/rpc", self._ws_handler),  # streams/push (live logs, stats)
        ])
        self._runner: Optional[web.AppRunner] = None
        self.started_at: Optional[float] = None
        self._ws_clients: Set[web.WebSocketResponse] = set()
        # Channel subscriptions: channel -> set(ws)
        self._subscriptions: Dict[str, Set[web.WebSocketResponse]] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.started_at = time.time()
        log.info("RPC gateway running on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        if self._runner is not None:
            await self._runner.cleanup()
        log.info("RPC gateway stopped")

    # ------------------------------------------------------------------ #
    # Runtime reconfiguration (owner commands)
    # ------------------------------------------------------------------ #
    def update_token(self, token: str) -> None:
        """Apply a rotated token without restarting the gateway."""
        self.token = token

    def update_cors_origins(self, origins: Iterable[str]) -> None:
        """Apply a changed CORS allow-list without restarting the gateway."""
        self.cors_origins = set(origins or [])

    def set_request_logging(self, enabled: bool) -> None:
        """Toggle request logging at runtime."""
        self.request_log_enabled = bool(enabled)

    # ------------------------------------------------------------------ #
    # Auth / CORS
    # ------------------------------------------------------------------ #
    def _check_token(self, provided: Optional[str]) -> bool:
        if not provided or not self.token:
            return False
        return hmac.compare_digest(provided, self.token)

    def _cors_headers(self, request: web.Request) -> Dict[str, str]:
        """CORS headers for an allowed Origin; empty dict otherwise."""
        origin = request.headers.get("Origin")
        if origin and origin in self.cors_origins:
            return {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Headers":
                    "Content-Type, X-Dashboard-Token, X-User-Id, X-Guild-Id",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Vary": "Origin",
            }
        return {}

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        cors = self._cors_headers(request)
        # CORS preflight: answer before auth (browsers do not send custom
        # headers on preflight requests).
        if request.method == "OPTIONS":
            return web.Response(status=204, headers=cors)
        if request.path == "/api/health":
            response = await handler(request)
        elif request.path == "/rpc" and request.method == "GET":
            # ONLY the WebSocket upgrade (GET /rpc) authenticates in the
            # connection_init frame. POST /rpc (request/response) MUST carry the token.
            response = await handler(request)
        elif not self._check_token(request.headers.get("X-Dashboard-Token")):
            response = web.json_response({"error": "unauthorized"}, status=401)
        else:
            response = await handler(request)
        for k, v in cors.items():
            if k not in response.headers:
                response.headers[k] = v
        return response

    # ------------------------------------------------------------------ #
    # Request logging (auditing, optional)
    # ------------------------------------------------------------------ #
    async def _dispatch(self, data: Any, transport: str) -> Optional[Dict[str, Any]]:
        """Dispatch a JSON-RPC message and (optionally) record it for auditing."""
        t0 = time.monotonic()
        response = await dispatcher.dispatch(self, data)
        if self.request_log_enabled and isinstance(data, dict):
            try:
                self._record_request(data, response, (time.monotonic() - t0) * 1000, transport)
            except Exception:
                log.debug("Request logging failed", exc_info=True)
        return response

    def _record_request(self, data: Dict[str, Any], response: Optional[Dict[str, Any]],
                        duration_ms: float, transport: str) -> None:
        auth = (data.get("params") or {}).get("auth") or {}
        error = (response or {}).get("error") if isinstance(response, dict) else None
        entry = {
            "time": time.time(),
            "transport": transport,           # "http" | "ws"
            "method": str(data.get("method")),
            "user_id": auth.get("user_id"),
            "guild_id": auth.get("guild_id"),
            "duration_ms": round(duration_ms, 1),
            "ok": error is None,
            "error_code": (error or {}).get("code") if isinstance(error, dict) else None,
        }
        self.request_log.append(entry)
        log.info(
            "RPC %s %s user=%s guild=%s %.1fms %s",
            transport, entry["method"], entry["user_id"], entry["guild_id"],
            duration_ms, "ok" if entry["ok"] else f"error({entry['error_code']})",
        )

    # ------------------------------------------------------------------ #
    # REST
    # ------------------------------------------------------------------ #
    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "bot_ready": self.bot.is_ready(),
            "time": time.time(),
        })

    async def _manifest_rest(self, request: web.Request) -> web.Response:
        # auth already handled via middleware; user context via query/header
        user_id = request.headers.get("X-User-Id")
        guild_id = request.headers.get("X-Guild-Id")
        params = {"auth": {"user_id": user_id, "guild_id": guild_id}}
        result = await self._dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "manifest.get", "params": params,
        }, "http")
        return web.json_response(result)

    async def _rpc_post(self, request: web.Request) -> web.Response:
        """HTTP variant of the JSON-RPC dispatcher (request/response).

        Expects a single JSON-RPC 2.0 request in the body. Auth via middleware.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "parse error"}}, status=400)
        if isinstance(data, list):  # Batch
            results = [r for r in
                       [await self._dispatch(m, "http") for m in data] if r is not None]
            return web.json_response(results)
        response = await self._dispatch(data, "http")
        return web.json_response(response if response is not None else {})

    # ------------------------------------------------------------------ #
    # WebSocket / JSON-RPC
    # ------------------------------------------------------------------ #
    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        authenticated = False
        self._ws_clients.add(ws)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "parse error"}})
                    continue

                # first frame must be connection_init with token
                if not authenticated:
                    if data.get("method") == "connection_init" and \
                            self._check_token((data.get("params") or {}).get("token")):
                        authenticated = True
                        await ws.send_json({"jsonrpc": "2.0", "id": data.get("id"),
                                            "result": {"ok": True}})
                        continue
                    await ws.send_json({"jsonrpc": "2.0", "id": data.get("id"),
                                        "error": {"code": UNAUTHORIZED, "message": "unauthorized"}})
                    await ws.close()
                    break

                # subscription control for push streams
                method = data.get("method")
                if method == "subscribe":
                    channel = (data.get("params") or {}).get("channel")
                    self._subscriptions.setdefault(channel, set()).add(ws)
                    await ws.send_json({"jsonrpc": "2.0", "id": data.get("id"),
                                        "result": {"subscribed": channel}})
                    continue
                if method == "unsubscribe":
                    channel = (data.get("params") or {}).get("channel")
                    self._subscriptions.get(channel, set()).discard(ws)
                    await ws.send_json({"jsonrpc": "2.0", "id": data.get("id"),
                                        "result": {"unsubscribed": channel}})
                    continue

                response = await self._dispatch(data, "ws")
                if response is not None:
                    await ws.send_json(response)
        finally:
            self._ws_clients.discard(ws)
            for subs in self._subscriptions.values():
                subs.discard(ws)
        return ws

    # ------------------------------------------------------------------ #
    # Push / streams (e.g. live logs, stats)
    # ------------------------------------------------------------------ #
    async def publish(self, channel: str, payload: Any) -> None:
        """Sends a notification to all subscribers of a channel."""
        subs = self._subscriptions.get(channel)
        if not subs:
            return
        message = {"jsonrpc": "2.0", "method": "stream", "params":
                   {"channel": channel, "data": payload}}
        dead = []
        for ws in subs:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            subs.discard(ws)

    # ------------------------------------------------------------------ #
    # Audit
    # ------------------------------------------------------------------ #
    def audit(self, action: str, ctx: Any, detail: Dict[str, Any]) -> None:
        entry = {
            "action": action,
            "user": str(getattr(ctx.user, "id", None)),
            "guild": str(getattr(ctx.guild, "id", None)) if ctx.guild else None,
            "detail": detail,
            "time": time.time(),
        }
        log.info("AUDIT %s", entry)
        if self._audit_sink is not None:
            try:
                asyncio.create_task(self._audit_sink(entry))
            except Exception:
                pass
