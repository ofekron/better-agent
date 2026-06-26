"""Typed manifest builders for extension UI hooks.

Extension authors declare UI surfaces — a session quick button, or a page with
an icon and optional number badge — through these helpers and embed the result
under ``entrypoints`` in their ``better-agent-extension.json``::

    from better_agent_sdk import Badge, HookAction, Page, QuickButton

    quick_button = QuickButton(
        label="Ask",
        icon="search",
        action=HookAction.ensure(
            endpoint="/api/extensions/ofek-dev.ask/backend/ask/ensure",
            path_template="/s/{session_id}",
        ),
    )
    page = Page(
        label="My page",
        icon="clipboard",
        open=HookAction.ensure(
            endpoint="/api/extensions/<extension-id>/backend/<path>/ensure",
            path_template="/s/{session_id}",
            include_cwd=True,
        ),
        badge=Badge(endpoint="/api/extensions/<extension-id>/backend/<path>/total"),
    )

The serialized dicts match core's manifest schema (validated by
``extension_store.validate_manifest``), so an author can drop them straight
into the manifest JSON.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

HookActionType = Literal["navigate", "ensure", "module"]
InstructionLevel = Literal["global", "project"]
FrontendModuleKind = Literal["module", "iframe"]


@dataclass(frozen=True)
class HookAction:
    """Click handler shared by a quick button and a page's ``open``.

    - ``navigate``: go to a frontend route.
    - ``ensure``: POST a backend endpoint (best-effort), then navigate to a
      route built by substituting ``{id_field}`` into ``path_template``.
    - ``module``: mount a frontend module (site-relative ``module_url``).
    """

    type: HookActionType
    path: str | None = None
    endpoint: str | None = None
    path_template: str | None = None
    id_field: str = "session_id"
    include_cwd: bool = False
    module_url: str | None = None

    @staticmethod
    def navigate(path: str) -> "HookAction":
        return HookAction(type="navigate", path=path)

    @staticmethod
    def ensure(
        endpoint: str,
        path_template: str,
        *,
        id_field: str = "session_id",
        include_cwd: bool = False,
    ) -> "HookAction":
        return HookAction(
            type="ensure",
            endpoint=endpoint,
            path_template=path_template,
            id_field=id_field,
            include_cwd=include_cwd,
        )

    @staticmethod
    def module(module_url: str) -> "HookAction":
        return HookAction(type="module", module_url=module_url)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.type == "navigate":
            data["path"] = self.path
        elif self.type == "ensure":
            data["endpoint"] = self.endpoint
            data["path_template"] = self.path_template
            data["id_field"] = self.id_field
            data["include_cwd"] = self.include_cwd
        elif self.type == "module":
            data["module_url"] = self.module_url
        return data


@dataclass(frozen=True)
class Badge:
    """A page's number badge, sourced from a ``GET endpoint`` → ``{count}``."""

    endpoint: str

    def to_dict(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint}


@dataclass(frozen=True)
class QuickButton:
    """A single session action button rendered in the desktop chat toolbar
    and the mobile top bar."""

    label: str
    action: HookAction
    icon: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"label": self.label, "action": self.action.to_dict()}
        if self.icon:
            data["icon"] = self.icon
        return data


@dataclass(frozen=True)
class Page:
    """A sidebar page icon with an optional number badge."""

    label: str
    open: HookAction
    id: str = "main"
    icon: str | None = None
    badge: Badge | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "label": self.label, "open": self.open.to_dict()}
        if self.icon:
            data["icon"] = self.icon
        if self.badge is not None:
            data["badge"] = self.badge.to_dict()
        return data


@dataclass(frozen=True)
class Setting:
    """A user-configurable field surfaced in Settings (manifest
    ``entrypoints.settings``). ``secret`` type routes to the OS keychain."""

    key: str
    label: str
    type: str = "string"
    default: Any = None
    enum: tuple[Any, ...] | None = None
    help: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"key": self.key, "label": self.label, "type": self.type}
        if self.default is not None:
            data["default"] = self.default
        if self.enum:
            data["enum"] = list(self.enum)
        if self.help:
            data["help"] = self.help
        return data


@dataclass(frozen=True)
class SmokeTest:
    required_paths: tuple[str, ...] = ("better-agent-extension.json",)
    python_modules: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_paths": list(self.required_paths),
            "python_modules": list(self.python_modules),
        }


@dataclass(frozen=True)
class ExtensionProtocol:
    smoke_test: SmokeTest = SmokeTest()
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "smoke_test": self.smoke_test.to_dict(),
        }


@dataclass(frozen=True)
class PermissionSet:
    session_state: bool = False
    spawn_runs: bool = False
    internal_loopback: bool = False
    filesystem: bool = False
    network: bool = False
    secrets: bool = False
    provider_config: bool = False
    backend_routes: bool = False
    storage: bool = False
    in_process_execution: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            key: value
            for key, value in {
                "session_state": self.session_state,
                "spawn_runs": self.spawn_runs,
                "internal_loopback": self.internal_loopback,
                "filesystem": self.filesystem,
                "network": self.network,
                "secrets": self.secrets,
                "provider_config": self.provider_config,
                "backend_routes": self.backend_routes,
                "storage": self.storage,
                "in_process_execution": self.in_process_execution,
            }.items()
            if value
        }


@dataclass(frozen=True)
class McpPredicate:
    equals: dict[str, str] | None = None
    not_equals: dict[str, str] | None = None
    nonempty: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.equals:
            data["equals"] = dict(self.equals)
        if self.not_equals:
            data["not_equals"] = dict(self.not_equals)
        if self.nonempty:
            data["nonempty"] = list(self.nonempty)
        return data


@dataclass(frozen=True)
class McpServer:
    name: str
    python: str
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    user_facing: bool = True
    bare_allowed: bool = False
    requires_backend_auth: bool = True
    replaces_builtin: str | None = None
    predicate: McpPredicate | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "python": self.python,
            "user_facing": self.user_facing,
            "bare_allowed": self.bare_allowed,
            "requires_backend_auth": self.requires_backend_auth,
        }
        if self.args:
            data["args"] = list(self.args)
        if self.env:
            data["env"] = dict(self.env)
        if self.replaces_builtin:
            data["replaces_builtin"] = self.replaces_builtin
        if self.predicate is not None:
            data["predicate"] = self.predicate.to_dict()
        return data


@dataclass(frozen=True)
class Instruction:
    name: str
    path: str
    level: InstructionLevel = "global"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "path": self.path, "level": self.level}


@dataclass(frozen=True)
class TeamDefinition:
    name: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "path": self.path}


@dataclass(frozen=True)
class FrontendModule:
    slot: str
    label: str
    module: str
    id: str | None = None
    kind: FrontendModuleKind = "module"

    def to_dict(self) -> dict[str, str]:
        return {
            "slot": self.slot,
            "id": self.id or self.slot,
            "label": self.label,
            "kind": self.kind,
            "module": self.module,
        }
