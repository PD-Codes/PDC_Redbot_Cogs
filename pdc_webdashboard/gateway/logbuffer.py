"""In-memory ring buffer for recent log records.

The web dashboard's Log-Viewer reads from this. A single ``logging.Handler`` is
attached to the root logger while the WebDashboard cog is loaded and keeps the
last N records in memory (no disk writes, no extra dependencies). Detached again
on cog unload so reloading the cog never stacks duplicate handlers.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Any, Deque, Dict, List, Optional

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class _RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._records: Deque[Dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exc: Optional[str] = None
            if record.exc_info:
                try:
                    exc = logging.Formatter().formatException(record.exc_info)
                except Exception:
                    exc = None
            self._records.append(
                {
                    "time": record.created,
                    "level": record.levelname,
                    "levelno": record.levelno,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "exc": exc,
                }
            )
        except Exception:
            # Logging must never raise.
            pass

    def snapshot(self, *, min_level: int = 0, query: str = "", limit: int = 300) -> List[Dict[str, Any]]:
        q = (query or "").lower().strip()
        out: List[Dict[str, Any]] = []
        for r in reversed(self._records):  # newest first
            if r["levelno"] < min_level:
                continue
            if q and q not in (f"{r['message']} {r['logger']}").lower():
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out


# Module-level singleton shared by the cog (install/uninstall) and the gateway
# method (snapshot).
_handler: Optional[_RingBufferHandler] = None


def install(capacity: int = 500, level: int = logging.INFO) -> None:
    """Attach the ring-buffer handler to the root logger (idempotent)."""
    global _handler
    if _handler is not None:
        return
    _handler = _RingBufferHandler(capacity)
    _handler.setLevel(level)
    logging.getLogger().addHandler(_handler)


def uninstall() -> None:
    """Detach the handler (safe to call even if not installed)."""
    global _handler
    if _handler is not None:
        try:
            logging.getLogger().removeHandler(_handler)
        except Exception:
            pass
        _handler = None


def level_value(name: str) -> int:
    return _LEVELS.get((name or "").upper(), 0)


def snapshot(*, min_level: int = 0, query: str = "", limit: int = 300) -> List[Dict[str, Any]]:
    if _handler is None:
        return []
    return _handler.snapshot(min_level=min_level, query=query, limit=limit)
