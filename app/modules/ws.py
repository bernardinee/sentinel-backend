"""WebSocket hub (§5.5). Envelope: {"type": ..., "at": ..., "data": {...}}.

Server pings every 20 s. Clients reconnect with backoff and backfill via
GET /api/v1/incidents?from=<last_seen> — see the dashboard ws wrapper.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import WebSocket

log = logging.getLogger(__name__)

PING_INTERVAL_S = 20


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        log.info("WS client connected (%d total)", len(self._clients))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, type_: str, data: dict) -> None:
        message = json.dumps({
            "type": type_,
            "at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }, default=str)
        async with self._lock:
            clients = list(self._clients)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    async def keepalive(self, ws: WebSocket) -> None:
        """Ping loop; also drains incoming frames so pongs are processed."""
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await ws.send_text(json.dumps({
                "type": "ping",
                "at": datetime.now(timezone.utc).isoformat(),
                "data": {},
            }))


manager = ConnectionManager()
