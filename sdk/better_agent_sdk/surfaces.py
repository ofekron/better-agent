from __future__ import annotations

from dataclasses import dataclass
import inspect
import sys
import threading
import uuid
from typing import Any, Callable

from api_surface_sync import Operation, OperationRegistry, local_client
from api_surface_sync import OperationClient
from api_surface_sync.surfaces.mcp import add_tools
from api_surface_sync.surfaces.typer import add_commands
from pydantic import BaseModel, ConfigDict, Field, RootModel, create_model

from better_agent_sdk.runtime_transport import RuntimeTransport


class OperationResult(RootModel[Any]):
    pass


@dataclass(frozen=True)
class OperationSpec:
    name: str
    handler: Callable[..., Any]
    description: str = ""
    sensitive: bool = False
    operation: str = ""


def build_registry(specs: tuple[OperationSpec, ...]) -> OperationRegistry:
    registry = OperationRegistry()
    for spec in specs:
        request_model = request_model_for_callable(spec.name, spec.handler)

        async def invoke(request: BaseModel, operation_spec: OperationSpec = spec) -> Any:
            result = operation_spec.handler(**request.model_dump(by_alias=True))
            if inspect.isawaitable(result):
                result = await result
            return OperationResult(root=result)

        registry.register(
            Operation(
                name=spec.name,
                operation_id=spec.name,
                summary=spec.description or inspect.getdoc(spec.handler) or "",
                request_model=request_model,
                response_model=OperationResult,
                handler=invoke,
                metadata={"sensitive": spec.sensitive},
            )
        )
    return registry


class _BrokerExecutor:
    def __init__(
        self,
        specs: tuple[OperationSpec, ...],
        registry: OperationRegistry,
    ) -> None:
        self._operations = {
            spec.name: spec.operation
            for spec in specs
        }
        self._snapshot = registry.snapshot()
        self._generation = ""
        self._lock = threading.Lock()

    async def run(self, name: str, request: BaseModel) -> Any:
        import asyncio

        await asyncio.to_thread(self._ensure_contract)
        operation = self._operations[name]
        if not operation:
            raise RuntimeError(f"operation has no broker identity: {name}")
        response = await asyncio.to_thread(
            RuntimeTransport().request,
            {
                "version": 1,
                "kind": "invoke",
                "operation": operation,
                "payload": request.model_dump(mode="json", by_alias=True),
                "request_id": f"operation_{uuid.uuid4().hex}",
                "generation": self._generation,
            },
        )
        return OperationResult(root=response.get("result"))

    def _ensure_contract(self) -> None:
        if self._generation:
            return
        with self._lock:
            if self._generation:
                return
            response = RuntimeTransport().request(
                {"version": 1, "kind": "catalog"}
            )
            generation = str(response.get("generation") or "")
            schemas = response.get("schema")
            if not generation or not isinstance(schemas, dict):
                raise RuntimeError("runtime operation catalog is invalid")
            for surface_name, operation in self._operations.items():
                if not operation:
                    raise RuntimeError(
                        f"operation has no broker identity: {surface_name}"
                    )
                remote = schemas.get(operation)
                if not isinstance(remote, dict):
                    raise RuntimeError(
                        f"runtime operation is unavailable: {operation}"
                    )
                local = self._snapshot.get(surface_name).request_schema()
                if _schema_shape(local) != _schema_shape(remote.get("request")):
                    raise RuntimeError(
                        f"runtime operation schema changed: {operation}"
                    )
            self._generation = generation


def build_client(
    specs: tuple[OperationSpec, ...],
    *,
    local: bool = False,
) -> OperationClient:
    registry = build_registry(specs)
    if local:
        return local_client(registry)
    snapshot = registry.snapshot()
    return OperationClient(snapshot, _BrokerExecutor(specs, registry))


def build_mcp_server(
    name: str,
    specs: tuple[OperationSpec, ...],
    *,
    instructions: str = "",
    local: bool = False,
):
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(name, instructions=instructions or None)
    add_tools(server, build_client(specs, local=local))
    return server


def build_cli_app(name: str, specs: tuple[OperationSpec, ...], *, local: bool = False):
    import typer

    app = typer.Typer(name=name, no_args_is_help=True)
    add_commands(app, build_client(specs, local=local))
    return app


def run_mcp_or_cli(
    name: str,
    specs: tuple[OperationSpec, ...],
    *,
    instructions: str = "",
) -> int:
    if sys.argv[1:2] == ["cli"]:
        build_cli_app(name, specs)(args=sys.argv[2:])
        return 0
    if sys.argv[1:]:
        raise SystemExit("expected no arguments for MCP mode or 'cli' for CLI mode")
    build_mcp_server(name, specs, instructions=instructions).run("stdio")
    return 0


def specs_from_fastmcp(
    server: Any,
    *,
    operations: dict[str, str],
) -> tuple[OperationSpec, ...]:
    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        raise RuntimeError("FastMCP tool registry is unavailable")
    if set(tools) != set(operations):
        raise RuntimeError("FastMCP tool/operation mapping is incomplete")
    return tuple(
        OperationSpec(
            name=str(tool.name),
            handler=tool.fn,
            description=str(tool.description or ""),
            operation=operations[str(tool.name)],
        )
        for tool in tools.values()
    )


def proxy_fastmcp_tools(
    server: Any,
    *,
    name: str,
    operations: dict[str, str],
) -> tuple[OperationSpec, ...]:
    specs = specs_from_fastmcp(server, operations=operations)
    proxy = build_mcp_server(name, specs)
    server._tool_manager = proxy._tool_manager
    return specs


def run_cli_from_fastmcp(
    server: Any,
    *,
    name: str,
    operations: dict[str, str],
) -> int:
    specs = specs_from_fastmcp(server, operations=operations)
    build_cli_app(name, specs)(args=sys.argv[2:])
    return 0


def request_model_for_callable(
    operation_name: str,
    handler: Callable[..., Any],
) -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {}
    for parameter in inspect.signature(handler).parameters.values():
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise TypeError(f"operation handlers cannot use variadic parameters: {operation_name}")
        annotation = (
            Any if parameter.annotation is inspect.Parameter.empty else parameter.annotation
        )
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        field_name = parameter.name
        if field_name in dir(BaseModel):
            field_name = f"input_{field_name}"
            default = Field(default, alias=parameter.name)
        fields[field_name] = (annotation, default)
    model_name = "".join(part.capitalize() for part in operation_name.split("_")) + "Request"
    return create_model(
        model_name,
        __config__=ConfigDict(extra="forbid", populate_by_name=True),
        **fields,
    )


def _schema_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _schema_shape(item)
            for key, item in value.items()
            if key not in {"description", "title"}
        }
    if isinstance(value, list):
        return [_schema_shape(item) for item in value]
    return value
