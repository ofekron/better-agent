from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
import uuid

from fastapi import WebSocket

from ws_subscription_contract import PRIORITY_OPENED, SUBSCRIBE_PRIORITIES


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
        # session_id -> {connection_id: priority ("opened" | "warm")}
        self._subscriptions: dict[str, dict[str, str]] = {}

    def attach(self, websocket: WebSocket) -> BffConnection:
        connection = BffConnection(websocket)
        with self._lock:
            self._connections[connection.id] = connection
        return connection

    def subscribe(
        self,
        connection: BffConnection,
        session_id: str,
        priority: str = PRIORITY_OPENED,
    ) -> None:
        if not session_id:
            return
        if priority not in SUBSCRIBE_PRIORITIES:
            raise ValueError(f"invalid subscriber priority: {priority!r}")
        with self._lock:
            self._subscriptions.setdefault(session_id, {})[connection.id] = priority

    def unsubscribe(self, connection: BffConnection, session_id: str) -> None:
        with self._lock:
            ids = self._subscriptions.get(session_id)
            if ids is None:
                return
            ids.pop(connection.id, None)
            if not ids:
                self._subscriptions.pop(session_id, None)

    def subscriber_priorities(self, session_id: str) -> dict[str, str]:
        """Registry view: connection_id -> "opened" | "warm" for a session."""
        with self._lock:
            return dict(self._subscriptions.get(session_id, {}))

    def detach(self, connection: BffConnection) -> None:
        with self._lock:
            self._connections.pop(connection.id, None)
            for session_id, ids in list(self._subscriptions.items()):
                ids.pop(connection.id, None)
                if not ids:
                    self._subscriptions.pop(session_id, None)

    async def _publish(
        self,
        targets: list[BffConnection],
        event: dict[str, Any],
    ) -> None:
        results = await asyncio.gather(
            *(target.send_event(event) for target in targets),
            return_exceptions=True,
        )
        for target, result in zip(targets, results):
            if isinstance(result, Exception):
                self.detach(target)

    async def publish_global(self, event: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._connections.values())
        await self._publish(targets, event)

    async def publish_session(self, session_id: str, event: dict[str, Any]) -> None:
        # Opened subscribers first, warm after — warm is deprioritized in
        # send order but receives every frame (never dropped).
        with self._lock:
            targets = [
                self._connections[connection_id]
                for connection_id, priority in sorted(
                    self._subscriptions.get(session_id, {}).items(),
                    key=lambda entry: entry[1] != PRIORITY_OPENED,
                )
                if connection_id in self._connections
            ]
        await self._publish(targets, event)


hub = BffEventHub()
