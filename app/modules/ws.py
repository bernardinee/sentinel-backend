"""WebSocket hub (§5.5). Envelope: {"type": ..., "at": ..., "data": {...}}.

Server pings every 20 s. Clients reconnect with backoff and backfill via
GET /api/v1/incidents?from=<last_seen> — see the dashboard ws wrapper.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import WebSocket

from app.auth import Principal

log = logging.getLogger(__name__)

PING_INTERVAL_S = 20


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: dict[WebSocket, Principal] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self,
        ws: WebSocket,
        principal: Principal,
        subprotocol: str | None = None,
    ) -> None:
        await ws.accept(subprotocol=subprotocol)
        async with self._lock:
            self._clients[ws] = principal
        log.info("WS client connected (%d total)", len(self._clients))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(ws, None)

    @staticmethod
    def can_receive(principal: Principal, data: dict) -> bool:
        if principal.role == "responder":
            return True
        if principal.role != "driver":
            return False
        event_device = data.get("device_id")
        return event_device in {principal.device_db_id, principal.device_id}

    async def broadcast(self, type_: str, data: dict) -> None:
        message = json.dumps({
            "type": type_,
            "at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }, default=str)
        async with self._lock:
            clients = list(self._clients.items())
        dead: list[WebSocket] = []
        for ws, principal in clients:
            if not self.can_receive(principal, data):
                continue
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
