from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import inspect
import json
from pathlib import Path
import re
import threading
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping

from api_surface_sync import Operation, OperationClient, OperationRegistry, RegistrySnapshot
from pydantic import BaseModel, RootModel
from paths import bc_home
from json_store import write_json

Handler = Callable[[BaseModel], Any | Awaitable[Any]]
RecoveryHandler = Callable[[BaseModel, str | None, str], Any | Awaitable[Any]]
_ARTIFACT_DIGEST_CACHE: dict[str, tuple[str, str]] = {}


class SideEffectClass(str, Enum):
    READ = "read"
    MUTATION = "mutation"
    CONTROL = "control"
    COMPATIBILITY = "compatibility"


class RecoveryPolicy(str, Enum):
    RESUME = "resume"
    RECONCILE = "reconcile"
    FAIL = "fail"


class ExecutionOwner(str, Enum):
    PRIMARY = "primary"
    NODE = "node"
    EXTENSION = "extension"


@dataclass(frozen=True)
class OperationPolicy:
    side_effect: SideEffectClass
    owner: ExecutionOwner
    recovery: RecoveryPolicy
    durable: bool
    cancel_supported: bool
    context_required: bool
    resource_fields: tuple[str, ...] = ()

    @classmethod
    def compatibility(cls) -> OperationPolicy:
        return cls(
            side_effect=SideEffectClass.COMPATIBILITY,
            owner=ExecutionOwner.PRIMARY,
            recovery=RecoveryPolicy.FAIL,
            durable=False,
            cancel_supported=False,
            context_required=False,
            resource_fields=(),
        )

    def projection(self) -> dict[str, Any]:
        return {
            "side_effect": self.side_effect.value,
            "owner": self.owner.value,
            "recovery": self.recovery.value,
            "durable": self.durable,
            "cancel_supported": self.cancel_supported,
            "context_required": self.context_required,
            "resource_fields": list(self.resource_fields),
        }


class AnyOperationResponse(RootModel[Any]):
    pass


@dataclass(frozen=True)
class OperationDescriptor:
    key: str
    capability: str
    action: str
    request_model: type[BaseModel]
    handler: Handler
    recovery_handler: RecoveryHandler | None
    policy: OperationPolicy
    artifact_root: str
    artifact_identity: str
    artifact_digest: str
    handler_identity: str

    def execution_projection(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "capability": self.capability,
            "action": self.action,
            "artifact_identity": self.artifact_identity,
            "artifact_digest": self.artifact_digest,
            "handler_identity": self.handler_identity,
            "recovery_handler_identity": (
                f"{self.recovery_handler.__module__}:{self.recovery_handler.__qualname__}"
                if self.recovery_handler is not None
                else None
            ),
            "policy": self.policy.projection(),
        }


@dataclass(frozen=True)
class PublishedCatalog:
    generation: str
    snapshot: RegistrySnapshot
    descriptors: Mapping[str, OperationDescriptor]
    client: OperationClient

    def descriptor(self, key: str) -> OperationDescriptor:
        try:
            return self.descriptors[key]
        except KeyError as exc:
            raise KeyError(f"unknown operation: {key}") from exc

    def capability_descriptor(self, capability: str, action: str) -> OperationDescriptor:
        return self.descriptor(operation_key(capability, action))

    def verify_artifacts(self) -> None:
        verified: dict[str, str] = {}
        for descriptor in self.descriptors.values():
            actual = verified.get(descriptor.artifact_root)
            if actual is None:
                actual = _artifact_digest(Path(descriptor.artifact_root))
                verified[descriptor.artifact_root] = actual
            if actual != descriptor.artifact_digest:
                raise RuntimeError(f"operation artifact changed: {descriptor.key}")


class _CatalogExecutor:
    def __init__(self, descriptors: Mapping[str, OperationDescriptor]) -> None:
        self._descriptors = descriptors

    async def run(self, name: str, request: BaseModel) -> Any:
        descriptor = self._descriptors[name]
        result = descriptor.handler(request)
        if inspect.isawaitable(result):
            result = await result
        return AnyOperationResponse(root=result)


class CatalogBuilder:
    def __init__(self, descriptors: Mapping[str, OperationDescriptor] | None = None) -> None:
        self._descriptors = dict(descriptors or {})

    def register_capability(
        self,
        capability: str,
        action: str,
        schema: type[BaseModel],
        handler: Handler,
        *,
        policy: OperationPolicy | None = None,
        recovery_handler: RecoveryHandler | None = None,
    ) -> OperationDescriptor:
        key = operation_key(capability, action)
        if key in self._descriptors:
            raise RuntimeError(f"duplicate capability action: {capability}.{action}")
        artifact_path = _handler_artifact_path(handler)
        artifact_root, artifact_identity = _artifact_root(artifact_path, handler)
        descriptor = OperationDescriptor(
            key=key,
            capability=capability,
            action=action,
            request_model=schema,
            handler=handler,
            recovery_handler=recovery_handler,
            policy=policy or OperationPolicy.compatibility(),
            artifact_root=str(artifact_root),
            artifact_identity=artifact_identity,
            artifact_digest=_artifact_digest(artifact_root, use_cache=True),
            handler_identity=f"{handler.__module__}:{handler.__qualname__}",
        )
        self._descriptors[key] = descriptor
        return descriptor

    def remove_capability(self, capability: str, action: str) -> None:
        key = operation_key(capability, action)
        if self._descriptors.pop(key, None) is None:
            raise KeyError((capability, action))

    def publish(self) -> PublishedCatalog:
        registry = OperationRegistry()
        for descriptor in sorted(self._descriptors.values(), key=lambda item: item.key):
            registry.register(
                Operation(
                    name=descriptor.key,
                    operation_id=f"{descriptor.capability}.{descriptor.action}",
                    summary="",
                    request_model=descriptor.request_model,
                    response_model=AnyOperationResponse,
                    handler=_forbidden_direct_handler,
                    metadata=descriptor.execution_projection(),
                )
            )
        snapshot = registry.snapshot()
        descriptors = MappingProxyType(dict(self._descriptors))
        generation = _execution_generation(snapshot, descriptors)
        client = OperationClient(snapshot, _CatalogExecutor(descriptors))
        return PublishedCatalog(
            generation=generation,
            snapshot=snapshot,
            descriptors=descriptors,
            client=client,
        )


class CatalogManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._builder = CatalogBuilder()
        self._current: PublishedCatalog | None = None
        self._generations: dict[str, PublishedCatalog] = {}
        self._pins: dict[str, int] = {}

    def register_capability(
        self,
        capability: str,
        action: str,
        schema: type[BaseModel],
        handler: Handler,
        *,
        policy: OperationPolicy | None = None,
        recovery_handler: RecoveryHandler | None = None,
    ) -> OperationDescriptor:
        with self._lock:
            if self._current is not None:
                _invalidate_handler_artifact(handler)
                self._builder = CatalogBuilder(self._current.descriptors)
                self._current = None
            return self._builder.register_capability(
                capability,
                action,
                schema,
                handler,
                policy=policy,
                recovery_handler=recovery_handler,
            )

    def replace_capability(
        self,
        capability: str,
        action: str,
        schema: type[BaseModel],
        handler: Handler,
        *,
        policy: OperationPolicy | None = None,
        recovery_handler: RecoveryHandler | None = None,
    ) -> OperationDescriptor:
        with self._lock:
            _invalidate_handler_artifact(handler)
            descriptors = dict(self.current().descriptors)
            descriptors.pop(operation_key(capability, action), None)
            self._builder = CatalogBuilder(descriptors)
            self._current = None
            descriptor = self._builder.register_capability(
                capability,
                action,
                schema,
                handler,
                policy=policy,
                recovery_handler=recovery_handler,
            )
            self.publish()
            return descriptor

    def remove_capability(self, capability: str, action: str) -> None:
        with self._lock:
            self._builder = CatalogBuilder(self.current().descriptors)
            self._current = None
            self._builder.remove_capability(capability, action)
            self.publish()

    def publish(self) -> PublishedCatalog:
        with self._lock:
            if self._current is None:
                catalog = self._builder.publish()
                existing = self._generations.get(catalog.generation)
                self._current = existing or catalog
                self._generations.setdefault(catalog.generation, catalog)
                _seal_catalog(self._current)
            return self._current

    def current(self) -> PublishedCatalog:
        return self.publish()

    def get(self, generation: str) -> PublishedCatalog:
        with self._lock:
            try:
                return self._generations[generation]
            except KeyError as exc:
                raise KeyError(f"unknown execution generation: {generation}") from exc

    def pin(self, generation: str) -> None:
        with self._lock:
            self.get(generation).verify_artifacts()
            self._pins[generation] = self._pins.get(generation, 0) + 1

    def unpin(self, generation: str) -> None:
        with self._lock:
            count = self._pins.get(generation, 0)
            if count <= 1:
                self._pins.pop(generation, None)
                return
            self._pins[generation] = count - 1

    def pin_count(self, generation: str) -> int:
        with self._lock:
            return self._pins.get(generation, 0)

    def restore_pins(self, counts: Mapping[str, int]) -> None:
        with self._lock:
            restored: dict[str, int] = {}
            for generation, count in counts.items():
                if count < 1:
                    continue
                self.get(generation).verify_artifacts()
                restored[generation] = count
            self._pins = restored


_MANAGER = CatalogManager()


def register_capability(
    capability: str,
    action: str,
    schema: type[BaseModel],
    handler: Handler,
    *,
    policy: OperationPolicy | None = None,
    recovery_handler: RecoveryHandler | None = None,
) -> OperationDescriptor:
    return _MANAGER.register_capability(
        capability,
        action,
        schema,
        handler,
        policy=policy,
        recovery_handler=recovery_handler,
    )


def publish() -> PublishedCatalog:
    return _MANAGER.publish()


def current() -> PublishedCatalog:
    return _MANAGER.current()


def manager() -> CatalogManager:
    return _MANAGER


def operation_key(capability: str, action: str) -> str:
    raw = f"{capability}_{action}".lower()
    key = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if not key or not key[0].isalpha():
        raise ValueError(f"invalid capability action identity: {capability}.{action}")
    return key


def _forbidden_direct_handler(_request: BaseModel) -> Any:
    raise RuntimeError("operation handlers are reachable only through the catalog executor")


def _handler_artifact_path(handler: Handler) -> Path:
    source = inspect.getsourcefile(handler) or inspect.getfile(handler)
    path = Path(source).resolve()
    if not path.is_file():
        raise RuntimeError(f"operation handler artifact is not a file: {path}")
    return path


def _invalidate_handler_artifact(handler: Handler) -> None:
    path = _handler_artifact_path(handler)
    root, _identity = _artifact_root(path, handler)
    _ARTIFACT_DIGEST_CACHE.pop(str(root), None)


def _artifact_root(path: Path, handler: Handler) -> tuple[Path, str]:
    for parent in (path.parent, *path.parents):
        if (
            (parent / "AGENTS.md").is_file()
            and (parent / "backend").is_dir()
            and path.is_relative_to(parent / "backend")
        ):
            return parent, "better-agent-runtime"
        if (parent / "better-agent-extension.json").is_file():
            return parent, f"extension:{parent.name}"
    package = handler.__module__.split(".", 1)[0]
    return path.parent, f"python-package:{package}"


def _artifact_digest(root: Path, *, use_cache: bool = False) -> str:
    cache_key = str(root)
    if use_cache and cache_key in _ARTIFACT_DIGEST_CACHE:
        return _ARTIFACT_DIGEST_CACHE[cache_key][1]
    digest = hashlib.sha256()
    try:
        files = _artifact_files(root)
        if not files:
            raise RuntimeError(f"operation artifact contains no executable files: {root}")
        state_digest = hashlib.sha256()
        for path in files:
            stat = path.stat()
            relative = path.relative_to(root).as_posix().encode()
            state_digest.update(relative)
            state_digest.update(stat.st_size.to_bytes(8, "big"))
            state_digest.update(stat.st_mtime_ns.to_bytes(8, "big"))
        state = state_digest.hexdigest()
        cached = _ARTIFACT_DIGEST_CACHE.get(cache_key)
        if cached is not None and cached[0] == state:
            return cached[1]
        for path in files:
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            content_digest = hashlib.sha256(path.read_bytes()).digest()
            digest.update(content_digest)
        value = digest.hexdigest()
        if use_cache:
            _ARTIFACT_DIGEST_CACHE[cache_key] = (state, value)
        return value
    except OSError as exc:
        raise RuntimeError(f"cannot read operation artifact: {root}") from exc


def _artifact_files(root: Path) -> list[Path]:
    if (root / "AGENTS.md").is_file() and (root / "backend").is_dir():
        candidates = (
            list((root / "backend").rglob("*.py"))
            + list((root / "extensions").rglob("*.py"))
            + list((root / "extensions").rglob("*.json"))
            + list((root / "vendor").rglob("*"))
            + [root / "backend" / "requirements.txt"]
        )
    else:
        candidates = list(root.rglob("*"))
    return sorted(
        path
        for path in candidates
        if path.is_file()
        and "__pycache__" not in path.parts
        and (
            path.suffix in {".py", ".json", ".toml", ".whl", ".tgz", ".txt"}
            or path.name in {"SOURCE_COMMIT", "SHA256SUMS"}
        )
    )


def _execution_generation(
    snapshot: RegistrySnapshot,
    descriptors: Mapping[str, OperationDescriptor],
) -> str:
    payload = {
        "contract": snapshot.schema(),
        "execution": [
            descriptors[key].execution_projection()
            for key in sorted(descriptors)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _seal_catalog(catalog: PublishedCatalog) -> None:
    root = bc_home() / "operation_catalog" / "generations"
    path = root / f"{catalog.generation}.json"
    projection = {
        "generation": catalog.generation,
        "contract": catalog.snapshot.schema(),
        "execution": [
            catalog.descriptors[key].execution_projection()
            for key in sorted(catalog.descriptors)
        ],
    }
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("operation catalog generation seal is corrupt") from exc
        if existing != projection:
            raise RuntimeError("operation catalog generation seal does not match runtime")
        return
    root.mkdir(parents=True, exist_ok=True)
    write_json(path, projection)
