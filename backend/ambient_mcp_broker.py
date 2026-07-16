from __future__ import annotations

import asyncio
import os
import socket
import threading
from pathlib import Path
from typing import Any

import ambient_mcp_transport as transport


_CORE_SERVER_PERMISSIONS = {
    "ui": ("ui.open_file_panel", "ui.open_browser_panel", "ui.request_user_input"),
    "open-config-panel": ("config.open_panel",),
    "capabilities": ("capabilities.read", "capabilities.write"),
}


class AmbientMcpBroker:
    def __init__(self) -> None:
        self._listener: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connections: set[Any] = set()
        self._lock = threading.Lock()
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None
        if os.name == "nt":
            self._start_windows()
            return
        self._listener = transport.prepare_posix_listener()
        self._thread = threading.Thread(target=self._serve_posix, daemon=True, name="ambient-mcp-broker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)
        if os.name != "nt":
            try:
                Path(transport.endpoint()).unlink()
            except FileNotFoundError:
                pass

    def _serve_posix(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle_posix,
                args=(connection,),
                daemon=True,
                name="ambient-mcp-client",
            ).start()

    def _handle_posix(self, connection: socket.socket) -> None:
        principal_id = ""
        with self._lock:
            self._connections.add(connection)
        try:
            peer_user = transport.posix_peer_user(connection)
            stream = connection.makefile("rwb", buffering=0)
            request = transport.recv_json(stream)
            principal_id, credential = self._issue(request, peer_user)
            transport.send_json(stream, {"credential": credential, "principal_id": principal_id})
            while stream.read(1):
                pass
        except (ConnectionError, OSError, PermissionError, ValueError):
            pass
        finally:
            if principal_id:
                self._revoke(principal_id)
            with self._lock:
                self._connections.discard(connection)
            connection.close()

    def _issue(self, request: dict[str, Any], peer_user: str) -> tuple[str, str]:
        source_kind = str(request.get("source_kind") or "extension").strip()
        extension_id = str(request.get("extension_id") or "").strip()
        server_name = str(request.get("server_name") or "").strip()
        provider_id = str(request.get("provider_id") or "ambient").strip()
        if not server_name:
            raise ValueError("server is required")
        import ambient_principal

        if source_kind == "core":
            permissions = _CORE_SERVER_PERMISSIONS.get(server_name)
            if permissions is None:
                raise PermissionError("core ambient MCP is not registered")
            import ambient_mcp_policy_store
            if not ambient_mcp_policy_store.is_exposed(f"core:{server_name}"):
                raise PermissionError("core ambient MCP native exposure is not enabled")
            credential, principal = ambient_principal.registry.issue(
                extension_id="better-agent-core",
                server_name=server_name,
                permissions=permissions,
                os_user_id=peer_user,
                provider_id=provider_id,
                cwd=str(request.get("cwd") or ""),
                pid=int(request.get("pid") or 0),
                connection_bound=True,
                source_kind="core",
                core_server=server_name,
            )
            return principal.principal_id, credential
        if source_kind != "extension" or not extension_id:
            raise ValueError("extension is required")
        import extension_store

        record = extension_store.get_extension(extension_id)
        if not record or not extension_store.is_extension_active(extension_id):
            raise PermissionError("extension is not active")
        item = extension_store._harness_addition(record, "mcp", server_name)
        if item is None:
            raise PermissionError("extension MCP is not installed")
        policy = item.get("native_exposure") or {}
        if policy.get("allowed") is not True:
            raise PermissionError("extension MCP does not allow native exposure")
        import ambient_mcp_policy_store
        capability_id = f"extension:{extension_id}:{server_name}"
        if not ambient_mcp_policy_store.is_exposed(capability_id):
            raise PermissionError("extension MCP native exposure is not enabled")
        permissions = list(policy.get("permissions") or [])
        credential, principal = ambient_principal.registry.issue(
            extension_id=extension_id,
            server_name=server_name,
            permissions=permissions,
            os_user_id=peer_user,
            provider_id=provider_id,
            cwd=str(request.get("cwd") or ""),
            pid=int(request.get("pid") or 0),
            connection_bound=True,
        )
        return principal.principal_id, credential

    def _revoke(self, principal_id: str) -> None:
        import ambient_principal

        ambient_principal.registry.revoke(principal_id)
        self._release_principal_locks(principal_id)

    def revoke_extension(self, extension_id: str, *, server_name: str) -> None:
        import ambient_principal

        principals = ambient_principal.registry.revoke_extension(
            extension_id, server_name=server_name
        )
        for principal in principals:
            self._release_principal_locks(principal.principal_id)

    def _release_principal_locks(self, principal_id: str) -> None:
        if self._event_loop is None or self._event_loop.is_closed():
            return
        import coordination

        asyncio.run_coroutine_threadsafe(
            coordination.release_principal_locks(principal_id),
            self._event_loop,
        )

    def _start_windows(self) -> None:
        try:
            import ambient_mcp_windows
        except ImportError as exc:
            raise RuntimeError("secure Windows named-pipe support unavailable") from exc
        self._listener = ambient_mcp_windows.Listener(transport.endpoint(), self._handle_windows)
        self._listener.start()

    def _handle_windows(self, connection: Any, peer_sid: str) -> None:
        principal_id = ""
        with self._lock:
            self._connections.add(connection)
        try:
            request = connection.recv()
            principal_id, credential = self._issue(request, peer_sid)
            connection.send({"credential": credential, "principal_id": principal_id})
            while connection.recv_bytes():
                pass
        except (EOFError, OSError, PermissionError, ValueError):
            pass
        finally:
            if principal_id:
                self._revoke(principal_id)
            with self._lock:
                self._connections.discard(connection)
            connection.close()


broker = AmbientMcpBroker()
