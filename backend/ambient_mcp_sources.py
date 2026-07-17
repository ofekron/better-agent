from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Literal

import ambient_user_mcp_store
import ambient_mcp_policy_store
from env_compat import better_agent_runtime_env


Ownership = Literal["better-agent-core", "extension", "user"]


@dataclass(frozen=True)
class AmbientMcpCapability:
    id: str
    name: str
    launcher: dict[str, Any] | None
    policy: dict[str, Any]
    ownership: Ownership
    available: bool
    unavailable_reason: str | None = None

    @property
    def exposed(self) -> bool:
        return ambient_mcp_policy_store.is_exposed(self.id, available=self.available)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "exposed": self.exposed}


SourceAdapter = Callable[[], Iterable[AmbientMcpCapability]]
_ADAPTERS: dict[str, SourceAdapter] = {}


def register_adapter(name: str, adapter: SourceAdapter) -> None:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("ambient MCP adapter name is required")
    if clean_name in _ADAPTERS:
        raise ValueError(f"ambient MCP adapter already registered: {clean_name}")
    _ADAPTERS[clean_name] = adapter


def capabilities() -> list[AmbientMcpCapability]:
    projected: dict[str, AmbientMcpCapability] = {}
    for adapter_name in sorted(_ADAPTERS):
        for item in _ADAPTERS[adapter_name]():
            if item.id in projected:
                raise ValueError(f"duplicate ambient MCP capability id: {item.id}")
            projected[item.id] = item
    return [projected[key] for key in sorted(projected)]


def _extension_capabilities() -> Iterable[AmbientMcpCapability]:
    import extension_store

    configs = extension_store.eligible_native_mcp_launcher_server_configs(
        {}, interacts_with_user=True, bare=False
    )
    for name, launcher in configs.items():
        env = dict(launcher.get("env") or {})
        extension_id = str(env.get("BETTER_CLAUDE_EXTENSION_ID") or "").strip()
        server_name = str(env.get("BETTER_CLAUDE_EXTENSION_MCP_SERVER") or "").strip()
        if not extension_id or not server_name:
            continue
        yield AmbientMcpCapability(
            id=f"extension:{extension_id}:{server_name}",
            name=name,
            launcher=launcher,
            policy={"native_exposure": True, "authentication": "ambient_broker"},
            ownership="extension",
            available=True,
        )


def _core_capabilities() -> Iterable[AmbientMcpCapability]:
    launcher_script = Path(__file__).with_name("core_ambient_mcp_launcher.py")
    for capability_id, name, permissions in (
        ("core:ui", "ui", ["ui.open_file_panel", "ui.open_browser_panel", "ui.request_user_input"]),
        ("core:open-config-panel", "open-config-panel", ["config.open_panel"]),
        ("core:capabilities", "capabilities", ["capabilities.read", "capabilities.write"]),
    ):
        yield AmbientMcpCapability(
            id=capability_id,
            name=name,
            launcher={
                "command": sys.executable,
                "args": [str(launcher_script), name],
                "env": better_agent_runtime_env(),
            },
            policy={"native_exposure": True, "session_bound": True, "permissions": permissions},
            ownership="better-agent-core",
            available=True,
        )


def _user_capabilities() -> Iterable[AmbientMcpCapability]:
    for record in ambient_user_mcp_store.list_records():
        yield AmbientMcpCapability(
            id=f"user:{record['id']}",
            name=record["name"],
            launcher=record["launcher"],
            policy=record["policy"],
            ownership="user",
            available=record["enabled"],
            unavailable_reason=None if record["enabled"] else "Disabled by user",
        )


register_adapter("core", _core_capabilities)
register_adapter("extensions", _extension_capabilities)
register_adapter("users", _user_capabilities)
