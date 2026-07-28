from __future__ import annotations

import hashlib
import json
import os
import platform as host_platform
import re
import shlex
import shutil
import stat
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit


CONTRACT_SCHEMA = 1
_SECRET_NAMES = (
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "credential",
    "authorization",
)
_ALLOWED_ENVIRONMENT_SELECTORS = {"CODEX_HOME"}
_ALLOWED_CONFIG_KEYS = {
    "features.image_generation",
    "features.shell_snapshot",
    "model",
    "model_provider",
    "shell_environment_policy.exclude",
}
_SAFE_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{0,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ExecutionContractError(RuntimeError):
    pass


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _sha256_and_first_line_fd(fd: int) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    first_line = bytearray()
    line_complete = False
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        if not line_complete and len(first_line) < 4096:
            remaining = 4096 - len(first_line)
            prefix = chunk[:remaining]
            newline = prefix.find(b"\n")
            if newline >= 0:
                first_line.extend(prefix[:newline + 1])
                line_complete = True
            else:
                first_line.extend(prefix)
                if len(first_line) == 4096:
                    line_complete = True
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest(), bytes(first_line)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if type(value) is not str:
        raise ExecutionContractError(f"{key} must be a string")
    return value


def _required_integer(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if type(value) is not int:
        raise ExecutionContractError(f"{key} must be an integer")
    return value


def _required_string_list(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ExecutionContractError(f"{key} must be a string list")
    return tuple(value)


def _symlink_chain(path: Path) -> tuple[tuple[str, str], ...]:
    current = path
    seen: set[Path] = set()
    chain: list[tuple[str, str]] = []
    for _ in range(64):
        absolute = current.absolute()
        if absolute in seen:
            raise ExecutionContractError("provider executable symlink cycle")
        seen.add(absolute)
        if not current.is_symlink():
            return tuple(chain)
        target = os.readlink(current)
        chain.append((str(absolute), target))
        target_path = Path(target)
        current = (
            target_path
            if target_path.is_absolute()
            else current.parent / target_path
        )
    raise ExecutionContractError("provider executable symlink chain is too deep")


@dataclass(frozen=True)
class FileIdentity:
    requested_path: str
    resolved_path: str
    sha256: str
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    symlink_chain: tuple[tuple[str, str], ...] = ()

    @classmethod
    def capture(cls, raw_path: str | Path) -> FileIdentity:
        requested = Path(raw_path)
        if not requested.is_absolute():
            raise ExecutionContractError("authority file path must be absolute")
        try:
            chain_before = _symlink_chain(requested)
            resolved_before = requested.resolve(strict=True)
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(resolved_before, flags)
            try:
                stat_before = os.fstat(fd)
                if not stat.S_ISREG(stat_before.st_mode):
                    raise ExecutionContractError("authority file is unavailable")
                digest = _sha256_fd(fd)
                stat_after = os.fstat(fd)
                if stat_before != stat_after:
                    raise ExecutionContractError(
                        "authority file changed during identity capture",
                    )
                chain_after = _symlink_chain(requested)
                resolved_after = requested.resolve(strict=True)
                path_stat = resolved_after.stat()
                if (
                    chain_before != chain_after
                    or resolved_before != resolved_after
                    or path_stat.st_dev != stat_after.st_dev
                    or path_stat.st_ino != stat_after.st_ino
                ):
                    raise ExecutionContractError(
                        "authority path changed during identity capture",
                    )
            finally:
                os.close(fd)
            return cls(
                requested_path=str(requested.absolute()),
                resolved_path=str(resolved_after),
                sha256=digest,
                size=stat_after.st_size,
                mtime_ns=stat_after.st_mtime_ns,
                ctime_ns=stat_after.st_ctime_ns,
                device=stat_after.st_dev,
                inode=stat_after.st_ino,
                symlink_chain=chain_after,
            )
        except ExecutionContractError:
            raise
        except OSError as exc:
            raise ExecutionContractError("authority file is unavailable") from exc

    def attest(self) -> bool:
        try:
            return FileIdentity.capture(self.requested_path) == self
        except ExecutionContractError:
            return False

    def attest_metadata(self) -> bool:
        try:
            current = FileIdentity.capture_metadata(self.requested_path)
        except ExecutionContractError:
            return False
        return current == (
            self.resolved_path,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
            self.device,
            self.inode,
            self.symlink_chain,
        )

    @classmethod
    def capture_metadata(
        cls,
        raw_path: str | Path,
    ) -> tuple[str, int, int, int, int, int, tuple[tuple[str, str], ...]]:
        requested = Path(raw_path)
        if not requested.is_absolute():
            raise ExecutionContractError("authority file path must be absolute")
        try:
            chain = _symlink_chain(requested)
            resolved = requested.resolve(strict=True)
            stat_result = resolved.stat()
        except OSError as exc:
            raise ExecutionContractError("authority file is unavailable") from exc
        if not resolved.is_file():
            raise ExecutionContractError("authority file is unavailable")
        return (
            str(resolved),
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
            stat_result.st_dev,
            stat_result.st_ino,
            chain,
        )


def _file_identity_from_dict(raw: Mapping[str, Any]) -> FileIdentity:
    expected = {
        "requested_path",
        "resolved_path",
        "sha256",
        "size",
        "mtime_ns",
        "ctime_ns",
        "device",
        "inode",
        "symlink_chain",
    }
    if set(raw) != expected:
        raise ExecutionContractError("invalid file identity")
    requested_path = _required_string(raw, "requested_path")
    resolved_path = _required_string(raw, "resolved_path")
    sha256 = _required_string(raw, "sha256")
    integers = {
        key: _required_integer(raw, key)
        for key in ("size", "mtime_ns", "ctime_ns", "device", "inode")
    }
    symlink_chain_raw = raw.get("symlink_chain")
    if (
        type(symlink_chain_raw) is not list
        or any(
            type(item) is not list
            or len(item) != 2
            or any(type(value) is not str for value in item)
            for item in symlink_chain_raw
        )
    ):
        raise ExecutionContractError("invalid file identity symlink chain")
    if (
        not Path(requested_path).is_absolute()
        or not Path(resolved_path).is_absolute()
        or not _SHA256_RE.fullmatch(sha256)
        or any(value < 0 for value in integers.values())
    ):
        raise ExecutionContractError("invalid file identity")
    return FileIdentity(
        requested_path=requested_path,
        resolved_path=resolved_path,
        sha256=sha256,
        symlink_chain=tuple(
            (item[0], item[1]) for item in symlink_chain_raw
        ),
        **integers,
    )


@dataclass(frozen=True)
class ConfigIdentity:
    root_path: str
    config_path: str
    config_file: FileIdentity | None

    @classmethod
    def capture(
        cls,
        root_path: str | Path,
        config_path: str | Path,
    ) -> ConfigIdentity:
        root = Path(root_path)
        candidate = Path(config_path)
        if not root.is_absolute() or not candidate.is_absolute():
            raise ExecutionContractError("config paths must be absolute")
        try:
            canonical_root = root.resolve(strict=True)
        except OSError as exc:
            raise ExecutionContractError("config root is unavailable") from exc
        lexical = candidate.absolute()
        if not lexical.is_relative_to(root.absolute()):
            raise ExecutionContractError("config path escapes its root")
        try:
            resolved_parent = candidate.parent.resolve(strict=True)
        except OSError as exc:
            raise ExecutionContractError("config parent is unavailable") from exc
        if not resolved_parent.is_relative_to(canonical_root):
            raise ExecutionContractError("config path escapes its root")
        if candidate.exists() or candidate.is_symlink():
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as exc:
                raise ExecutionContractError("config path is unavailable") from exc
            if not resolved.is_relative_to(canonical_root):
                raise ExecutionContractError("config path escapes its root")
            identity = FileIdentity.capture(candidate)
        else:
            identity = None
        return cls(
            root_path=str(canonical_root),
            config_path=str(lexical),
            config_file=identity,
        )

    def attest(self) -> bool:
        root = Path(self.root_path)
        candidate = Path(self.config_path)
        try:
            if root.resolve(strict=True) != root:
                return False
            current = ConfigIdentity.capture(root, candidate)
        except ExecutionContractError:
            return False
        return current == self

    def attest_metadata(self) -> bool:
        root = Path(self.root_path)
        candidate = Path(self.config_path)
        try:
            if root.resolve(strict=True) != root:
                return False
            if not candidate.absolute().is_relative_to(root):
                return False
            resolved_parent = candidate.parent.resolve(strict=True)
            if not resolved_parent.is_relative_to(root):
                return False
        except OSError:
            return False
        if self.config_file is None:
            return not candidate.exists() and not candidate.is_symlink()
        return self.config_file.attest_metadata()


@dataclass(frozen=True)
class LaunchChain:
    logical_command: str
    mode: str
    launcher: FileIdentity
    argv_prefix: tuple[str, ...]
    components: tuple[FileIdentity, ...]
    component_argv_indexes: tuple[int, ...]

    def attest(self) -> bool:
        return self.launcher.attest() and all(
            component.attest() for component in self.components
        )

    def attest_metadata(self) -> bool:
        return self.launcher.attest_metadata() and all(
            component.attest_metadata() for component in self.components
        )

    @contextmanager
    def open_attested_components(self) -> Iterator[tuple[int, ...]]:
        handles: list[int] = []
        try:
            for component in self.components:
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(component.resolved_path, flags)
                handles.append(fd)
                stat_result = os.fstat(fd)
                if (
                    stat_result.st_dev != component.device
                    or stat_result.st_ino != component.inode
                    or stat_result.st_size != component.size
                    or stat_result.st_mtime_ns != component.mtime_ns
                    or stat_result.st_ctime_ns != component.ctime_ns
                    or _sha256_fd(fd) != component.sha256
                ):
                    raise ExecutionContractError(
                        "execution component identity mismatch",
                    )
            yield tuple(handles)
        finally:
            for fd in handles:
                os.close(fd)


@dataclass(frozen=True)
class CodexExecutionContract:
    schema: int
    provider_id: str
    provider_kind: str
    provider_generation: str
    provider_record_version: int
    mode: str
    base_url: str
    profile: str
    credential_generation: int
    catalog_args: tuple[str, ...]
    runtime_args: tuple[str, ...]
    environment_selectors: tuple[tuple[str, str], ...]
    config: tuple[ConfigIdentity, ...]
    launch_chain: LaunchChain

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        if include_fingerprint:
            payload["fingerprint"] = hashlib.sha256(
                _canonical_json(payload),
            ).hexdigest()
        return payload

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.to_dict(include_fingerprint=False)),
        ).hexdigest()

    @property
    def catalog_fingerprint(self) -> str:
        payload = self.to_dict(include_fingerprint=False)
        payload.pop("runtime_args", None)
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CodexExecutionContract:
        expected = {
            "schema",
            "provider_id",
            "provider_kind",
            "provider_generation",
            "provider_record_version",
            "mode",
            "base_url",
            "profile",
            "credential_generation",
            "catalog_args",
            "runtime_args",
            "environment_selectors",
            "config",
            "launch_chain",
            "fingerprint",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ExecutionContractError("invalid execution contract")
        try:
            supplied_fingerprint = _required_string(raw, "fingerprint")
            launch_raw = raw["launch_chain"]
            if type(launch_raw) is not dict:
                raise ExecutionContractError("invalid launch chain")
            if set(launch_raw) != {
                "logical_command",
                "mode",
                "launcher",
                "argv_prefix",
                "components",
                "component_argv_indexes",
            }:
                raise ExecutionContractError("invalid launch chain")
            launcher = _file_identity_from_dict(launch_raw["launcher"])
            logical_command = _required_string(
                launch_raw,
                "logical_command",
            )
            launch_mode = _required_string(launch_raw, "mode")
            argv_prefix = _required_string_list(launch_raw, "argv_prefix")
            components_raw = launch_raw.get("components")
            if type(components_raw) is not list:
                raise ExecutionContractError("invalid launch components")
            components = tuple(
                _file_identity_from_dict(component)
                for component in components_raw
            )
            component_indexes_raw = launch_raw.get("component_argv_indexes")
            if (
                type(component_indexes_raw) is not list
                or any(type(index) is not int for index in component_indexes_raw)
            ):
                raise ExecutionContractError("invalid launch component indexes")
            component_indexes = tuple(component_indexes_raw)
            launch_chain = LaunchChain(
                logical_command=logical_command,
                mode=launch_mode,
                launcher=launcher,
                argv_prefix=argv_prefix,
                components=components,
                component_argv_indexes=component_indexes,
            )
            config_raw = raw.get("config")
            if type(config_raw) is not list:
                raise ExecutionContractError("invalid config identities")
            config = tuple(
                _config_identity_from_dict(item)
                for item in config_raw
            )
            selectors_raw = raw.get("environment_selectors")
            if (
                type(selectors_raw) is not list
                or any(
                    type(item) is not list
                    or len(item) != 2
                    or any(type(value) is not str for value in item)
                    for item in selectors_raw
                )
            ):
                raise ExecutionContractError(
                    "invalid environment selectors",
                )
            contract = cls(
                schema=_required_integer(raw, "schema"),
                provider_id=_required_string(raw, "provider_id"),
                provider_kind=_required_string(raw, "provider_kind"),
                provider_generation=_required_string(
                    raw,
                    "provider_generation",
                ),
                provider_record_version=_required_integer(
                    raw,
                    "provider_record_version",
                ),
                mode=_required_string(raw, "mode"),
                base_url=_required_string(raw, "base_url"),
                profile=_required_string(raw, "profile"),
                credential_generation=_required_integer(
                    raw,
                    "credential_generation",
                ),
                catalog_args=_required_string_list(raw, "catalog_args"),
                runtime_args=_required_string_list(raw, "runtime_args"),
                environment_selectors=tuple(
                    (item[0], item[1]) for item in selectors_raw
                ),
                config=config,
                launch_chain=launch_chain,
            )
        except ExecutionContractError:
            raise
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ExecutionContractError("invalid execution contract") from exc
        if (
            contract.schema != CONTRACT_SCHEMA
            or contract.provider_kind not in {"codex", "fugu"}
            or not contract.provider_id
            or not contract.provider_generation
            or contract.provider_record_version < 0
            or contract.credential_generation < 0
            or not _SAFE_PROFILE_RE.fullmatch(contract.profile)
            or not _SHA256_RE.fullmatch(supplied_fingerprint)
        ):
            raise ExecutionContractError("unsupported execution contract")
        _validate_launch_chain(contract.launch_chain)
        _clean_base_url(contract.base_url)
        _clean_config_args(contract.catalog_args)
        _clean_config_args(contract.runtime_args)
        _clean_environment_selectors(dict(contract.environment_selectors))
        if supplied_fingerprint != contract.fingerprint:
            raise ExecutionContractError("execution contract fingerprint mismatch")
        return contract

    def attest(self) -> bool:
        return self.launch_chain.attest() and all(
            identity.attest() for identity in self.config
        )

    def attest_metadata(self) -> bool:
        return self.launch_chain.attest_metadata() and all(
            identity.attest_metadata() for identity in self.config
        )


def _config_identity_from_dict(raw: Mapping[str, Any]) -> ConfigIdentity:
    if type(raw) is not dict or set(raw) != {
        "root_path",
        "config_path",
        "config_file",
    }:
        raise ExecutionContractError("invalid config identity")
    root_path = _required_string(raw, "root_path")
    config_path = _required_string(raw, "config_path")
    if not Path(root_path).is_absolute() or not Path(config_path).is_absolute():
        raise ExecutionContractError("invalid config identity")
    config_file_raw = raw.get("config_file")
    if config_file_raw is not None and type(config_file_raw) is not dict:
        raise ExecutionContractError("invalid config identity")
    return ConfigIdentity(
        root_path=root_path,
        config_path=config_path,
        config_file=(
            _file_identity_from_dict(config_file_raw)
            if config_file_raw
            else None
        ),
    )


def _validate_launch_chain(chain: LaunchChain) -> None:
    indexes = chain.component_argv_indexes
    if (
        chain.logical_command != "codex"
        or chain.mode not in {"native", "vendor", "shebang"}
        or Path(chain.launcher.requested_path).stem.lower() != "codex"
        or not chain.argv_prefix
        or not chain.components
        or len(indexes) != len(chain.components)
        or indexes != tuple(sorted(set(indexes)))
        or indexes[0] != 0
        or any(index < 0 or index >= len(chain.argv_prefix) for index in indexes)
    ):
        raise ExecutionContractError("incoherent launch chain")
    indexed = set(indexes)
    for index, component in zip(indexes, chain.components):
        if chain.argv_prefix[index] not in {
            component.requested_path,
            component.resolved_path,
        }:
            raise ExecutionContractError("incoherent launch chain")
    for index, value in enumerate(chain.argv_prefix):
        if "\x00" in value or "\n" in value:
            raise ExecutionContractError("incoherent launch chain")
        if Path(value).is_absolute() and index not in indexed:
            raise ExecutionContractError("unattested launch path")
        if index not in indexed and any(
            marker in value.lower() for marker in _SECRET_NAMES
        ):
            raise ExecutionContractError("secret launch argument is not allowed")
    if chain.mode in {"native", "vendor"}:
        if (
            len(chain.components) != 1
            or chain.component_argv_indexes != (0,)
            or chain.argv_prefix != (chain.components[0].resolved_path,)
        ):
            raise ExecutionContractError("incoherent native launch chain")
        return
    if len(chain.components) != 2:
        raise ExecutionContractError("incoherent shebang launch chain")
    interpreter, script = chain.components
    if script.resolved_path != chain.launcher.resolved_path:
        raise ExecutionContractError("incoherent shebang launch chain")
    tokens = _read_shebang_tokens(
        Path(chain.launcher.resolved_path),
        expected=chain.launcher,
    )
    if tokens[0] == "/usr/bin/env":
        if len(tokens) != 2 or tokens[1].startswith("-"):
            raise ExecutionContractError("provider env shebang is ambiguous")
        if Path(interpreter.resolved_path).name != tokens[1]:
            raise ExecutionContractError("incoherent shebang interpreter")
        expected = (interpreter.resolved_path, script.resolved_path)
    else:
        try:
            resolved_interpreter = Path(tokens[0]).resolve(strict=True)
        except OSError as exc:
            raise ExecutionContractError(
                "provider interpreter is unavailable",
            ) from exc
        if resolved_interpreter != Path(interpreter.resolved_path):
            raise ExecutionContractError("incoherent shebang interpreter")
        expected = (
            interpreter.resolved_path,
            *tokens[1:],
            script.resolved_path,
        )
    if chain.argv_prefix != expected:
        raise ExecutionContractError("incoherent shebang arguments")


def _vendor_candidates(
    launcher: Path,
    resolved_target: Path,
    platform: str,
    architecture: str,
) -> list[Path]:
    lowered = platform.lower()
    normalized_arch = architecture.lower()
    if normalized_arch in {"amd64", "x86_64"}:
        normalized_arch = "x64"
    elif normalized_arch == "aarch64":
        normalized_arch = "arm64"
    if lowered.startswith("win"):
        package_pattern = f"codex-win32-{normalized_arch}"
        executable_name = "codex.exe"
    elif lowered.startswith("darwin"):
        package_pattern = f"codex-darwin-{normalized_arch}"
        executable_name = "codex"
    elif lowered.startswith("linux"):
        package_pattern = f"codex-linux-{normalized_arch}"
        executable_name = "codex"
    else:
        return []
    package_roots: list[Path] = []
    for candidate in (
        launcher.parent / "node_modules" / "@openai" / "codex",
        resolved_target.parent,
        *resolved_target.parents,
    ):
        if candidate.name == "codex" and candidate.parent.name == "@openai":
            package_roots.append(candidate)
    candidates: set[Path] = set()
    for package_root in dict.fromkeys(package_roots):
        dependency_root = package_root / "node_modules" / "@openai"
        if not dependency_root.is_dir():
            continue
        for package in dependency_root.glob(package_pattern):
            vendor = package / "vendor"
            if not vendor.is_dir():
                continue
            for candidate in vendor.glob(f"**/{executable_name}"):
                if candidate.is_file():
                    candidates.add(candidate.resolve(strict=True))
    return sorted(candidates)


def _read_shebang_tokens(
    target: Path,
    *,
    expected: FileIdentity | None = None,
) -> tuple[str, ...]:
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags)
        try:
            stat_before = os.fstat(fd)
            digest, first_line = _sha256_and_first_line_fd(fd)
            stat_after = os.fstat(fd)
            if stat_before != stat_after:
                raise ExecutionContractError(
                    "provider launcher changed during shebang read",
                )
            if expected is not None and (
                stat_after.st_dev != expected.device
                or stat_after.st_ino != expected.inode
                or stat_after.st_size != expected.size
                or stat_after.st_mtime_ns != expected.mtime_ns
                or stat_after.st_ctime_ns != expected.ctime_ns
                or digest != expected.sha256
            ):
                raise ExecutionContractError(
                    "provider launcher identity mismatch",
                )
        finally:
            os.close(fd)
    except OSError as exc:
        raise ExecutionContractError("provider launcher is unreadable") from exc
    if not first_line.startswith(b"#!"):
        return ()
    try:
        tokens = shlex.split(first_line[2:].decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExecutionContractError("provider launcher shebang is invalid") from exc
    if not tokens:
        raise ExecutionContractError("provider launcher shebang is empty")
    return tuple(tokens)


def _shebang_prefix(
    target: Path,
    *,
    search_path: str | None,
    expected: FileIdentity,
) -> tuple[str, ...] | None:
    tokens = _read_shebang_tokens(target, expected=expected)
    if not tokens:
        return None
    interpreter = tokens[0]
    interpreter_args = list(tokens[1:])
    if interpreter == "/usr/bin/env":
        if len(interpreter_args) != 1 or interpreter_args[0].startswith("-"):
            raise ExecutionContractError("provider env shebang is ambiguous")
        resolved = shutil.which(
            interpreter_args[0],
            path=search_path or os.environ.get("PATH", ""),
        )
        if not resolved:
            raise ExecutionContractError("provider interpreter is unavailable")
        return (str(Path(resolved).resolve(strict=True)), str(target))
    interpreter_path = Path(interpreter)
    if not interpreter_path.is_absolute() or not interpreter_path.is_file():
        raise ExecutionContractError("provider interpreter is unavailable")
    return (
        str(interpreter_path.resolve(strict=True)),
        *interpreter_args,
        str(target),
    )


def resolve_codex_launch_chain(
    launcher_path: str,
    *,
    search_path: str | None = None,
    platform: str | None = None,
    architecture: str | None = None,
) -> LaunchChain:
    launcher = Path(launcher_path)
    if not launcher.is_absolute():
        raise ExecutionContractError("provider launcher must be absolute")
    launcher_identity = FileIdentity.capture(launcher)
    target = Path(launcher_identity.resolved_path)
    effective_platform = platform or sys.platform
    effective_architecture = architecture or host_platform.machine()
    vendors = _vendor_candidates(
        launcher,
        target,
        effective_platform,
        effective_architecture,
    )
    if len(vendors) > 1:
        raise ExecutionContractError("provider vendor target is ambiguous")
    if vendors:
        launch_mode = "vendor"
        argv_prefix = (str(vendors[0]),)
    elif effective_platform.lower().startswith("win") and launcher.suffix.lower() in {
        ".cmd",
        ".bat",
    }:
        raise ExecutionContractError("provider vendor target is unavailable")
    else:
        shebang_prefix = _shebang_prefix(
            target,
            search_path=search_path,
            expected=launcher_identity,
        )
        launch_mode = "shebang" if shebang_prefix else "native"
        argv_prefix = shebang_prefix or (str(target),)
    component_entries = [
        (index, Path(value))
        for index, value in enumerate(argv_prefix)
        if Path(value).is_absolute() and Path(value).is_file()
    ]
    chain = LaunchChain(
        logical_command="codex",
        mode=launch_mode,
        launcher=launcher_identity,
        argv_prefix=argv_prefix,
        components=tuple(
            FileIdentity.capture(path) for _index, path in component_entries
        ),
        component_argv_indexes=tuple(
            index for index, _path in component_entries
        ),
    )
    _validate_launch_chain(chain)
    return chain


def _clean_environment_selectors(
    raw: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    cleaned: list[tuple[str, str]] = []
    for key, value in sorted((raw or {}).items()):
        lowered = str(key).lower()
        if any(marker in lowered for marker in _SECRET_NAMES):
            raise ExecutionContractError("secret selector is not allowed")
        if str(key) not in _ALLOWED_ENVIRONMENT_SELECTORS:
            raise ExecutionContractError("environment selector is not allowed")
        cleaned.append((str(key), str(value)))
    return tuple(cleaned)


def _clean_config_args(raw: Sequence[str]) -> tuple[str, ...]:
    if type(raw) not in {list, tuple}:
        raise ExecutionContractError("config arguments must be a sequence")
    values = tuple(raw)
    if any(type(value) is not str for value in values) or len(values) % 2:
        raise ExecutionContractError("config arguments are invalid")
    cleaned: list[str] = []
    for index in range(0, len(values), 2):
        marker, assignment = values[index:index + 2]
        if marker != "-c" or "=" not in assignment:
            raise ExecutionContractError("config arguments are invalid")
        key, value = assignment.split("=", 1)
        if key not in _ALLOWED_CONFIG_KEYS or "\x00" in value or "\n" in value:
            raise ExecutionContractError("config override is not allowed")
        if key != "shell_environment_policy.exclude" and any(
            secret in assignment.lower() for secret in _SECRET_NAMES
        ):
            raise ExecutionContractError("secret config override is not allowed")
        cleaned.extend((marker, assignment))
    return tuple(cleaned)


def _clean_base_url(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ExecutionContractError("provider base URL contains credentials")
    return value


def build_codex_execution_contract(
    provider: Mapping[str, Any],
    *,
    launcher_path: str,
    profile: str | None = None,
    catalog_args: Sequence[str] = (),
    runtime_args: Sequence[str] = (),
    credential_generation: int = 0,
    environment_selectors: Mapping[str, str] | None = None,
    config_paths: Sequence[str] | None = None,
    search_path: str | None = None,
    platform: str | None = None,
    architecture: str | None = None,
) -> CodexExecutionContract:
    provider_id = str(provider.get("id") or "").strip()
    provider_kind = str(provider.get("kind") or "").strip()
    generation = str(provider.get("generation") or "").strip()
    try:
        record_version = int(provider.get("record_version"))
    except (TypeError, ValueError) as exc:
        raise ExecutionContractError("provider record version is invalid") from exc
    if not provider_id or provider_kind not in {"codex", "fugu"} or not generation:
        raise ExecutionContractError("provider authority is incomplete")
    if record_version < 0 or credential_generation < 0:
        raise ExecutionContractError("provider authority revision is invalid")
    cleaned_profile = str(profile or "")
    if not _SAFE_PROFILE_RE.fullmatch(cleaned_profile):
        raise ExecutionContractError("provider profile is invalid")
    config_dir_raw = str(provider.get("config_dir") or "").strip()
    config_dir = Path(config_dir_raw).expanduser() if config_dir_raw else Path.home() / ".codex"
    if not config_dir.is_absolute():
        raise ExecutionContractError("provider config root must be absolute")
    try:
        canonical_config_dir = config_dir.resolve(strict=True)
    except OSError as exc:
        raise ExecutionContractError("provider config root is unavailable") from exc
    observed_paths = (
        tuple(Path(path) for path in config_paths)
        if config_paths is not None
        else (canonical_config_dir / "config.toml",)
    )
    config = tuple(
        ConfigIdentity.capture(canonical_config_dir, path)
        for path in observed_paths
    )
    launch_chain = resolve_codex_launch_chain(
        launcher_path,
        search_path=search_path,
        platform=platform,
        architecture=architecture,
    )
    return CodexExecutionContract(
        schema=CONTRACT_SCHEMA,
        provider_id=provider_id,
        provider_kind=provider_kind,
        provider_generation=generation,
        provider_record_version=record_version,
        mode=str(provider.get("mode") or "subscription"),
        base_url=_clean_base_url(provider.get("base_url")),
        profile=cleaned_profile,
        credential_generation=int(credential_generation),
        catalog_args=_clean_config_args(catalog_args),
        runtime_args=_clean_config_args(runtime_args),
        environment_selectors=_clean_environment_selectors(environment_selectors),
        config=config,
        launch_chain=launch_chain,
    )
