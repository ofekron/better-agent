from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
import uuid

from fastapi import WebSocket


class BffConnection:
    def __init__(self, websocket: WebSocket) -> None:
        self.id = uuid.uuid4().hex
        self.websocket = websocket
        self._send_lock = asyncio.Lock()

    async def send_frame(self, frame: str | bytes) -> None:
        async with self._send_lock:
            if isinstance(frame, str):
                await self.websocket.send_text(frame)
            else:
                await self.websocket.send_bytes(frame)

    async def send_event(self, event: dict[str, Any]) -> None:
        await self.send_frame(json.dumps(event, separators=(",", ":")))


class BffEventHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._connections: dict[str, BffConnection] = {}
        self._subscriptions: dict[str, set[str]] = {}

    def attach(self, websocket: WebSocket) -> BffConnection:
        connection = BffConnection(websocket)
        with self._lock:
            self._connections[connection.id] = connection
        return connection

    def subscribe(self, connection: BffConnection, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._subscriptions.setdefault(session_id, set()).add(connection.id)

    def unsubscribe(self, connection: BffConnection, session_id: str) -> None:
        with self._lock:
            ids = self._subscriptions.get(session_id)
            if ids is None:
                return
            ids.discard(connection.id)
            if not ids:
                self._subscriptions.pop(session_id, None)

    def detach(self, connection: BffConnection) -> None:
        with self._lock:
            self._connections.pop(connection.id, None)
            for session_id, ids in list(self._subscriptions.items()):
                ids.discard(connection.id)
                if not ids:
                    self._subscriptions.pop(session_id, None)

    async def publish_session(self, session_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            targets = [
                self._connections[connection_id]
                for connection_id in self._subscriptions.get(session_id, set())
                if connection_id in self._connections
            ]
        results = await asyncio.gather(
            *(target.send_event(event) for target in targets),
            return_exceptions=True,
        )
        for target, result in zip(targets, results):
            if isinstance(result, Exception):
                self.detach(target)


hub = BffEventHub()
