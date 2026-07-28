from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence

from cli_paths import DEFAULT_CLI_DIRS
from model_catalog_authority import CatalogAuthority
from model_catalog_source_watcher import SourceWatchSpec
from paths import expand_user_path


_WINDOWS_CODEX_NAMES = ("codex", "codex.bat", "codex.cmd", "codex.exe")


def _identity_paths(authority: CatalogAuthority) -> set[Path]:
    contract = authority.execution_contract
    identities = {
        contract.launch_chain.launcher,
        *contract.launch_chain.components,
    }
    paths: set[Path] = set()
    for identity in identities:
        paths.add(Path(identity.requested_path))
        paths.add(Path(identity.resolved_path))
        paths.update(Path(link) for link, _target in identity.symlink_chain)
    for config in contract.config:
        paths.add(Path(config.config_path))
        paths.add(Path(config.root_path))
        if config.config_file is not None:
            paths.add(Path(config.config_file.requested_path))
            paths.add(Path(config.config_file.resolved_path))
            paths.update(
                Path(link)
                for link, _target in config.config_file.symlink_chain
            )
    return paths


def _identity_directories(authority: CatalogAuthority) -> set[Path]:
    return {
        Path(config.parent_path)
        for config in authority.execution_contract.config
    }


def _nearest_existing_directory(path: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    for directory in (candidate, *candidate.parents):
        if directory.is_dir():
            return directory
    return None


def _provider_config_root(
    provider: Mapping[str, object],
) -> Path | None:
    raw = str(provider.get("config_dir") or "").strip()
    if not raw:
        raw = os.environ.get("CODEX_HOME", "").strip()
    if not raw:
        return None
    root = expand_user_path(raw)
    if not root.is_absolute():
        return None
    return root


def _provider_config_paths(provider: Mapping[str, object]) -> set[Path]:
    root = _provider_config_root(provider)
    if root is None:
        return set()
    return {root, root / "config.toml", root / "auth.json"}


def _search_paths() -> tuple[set[Path], set[Path]]:
    raw_directories = [
        *os.environ.get("PATH", "").split(os.pathsep),
        *DEFAULT_CLI_DIRS,
    ]
    directories: set[Path] = set()
    exact: set[Path] = set()
    for raw in raw_directories:
        if not raw:
            continue
        candidate = expand_user_path(raw)
        if not candidate.is_absolute():
            continue
        if candidate.is_dir():
            directories.add(candidate)
            continue
        exact.add(candidate)
        current = candidate.parent
        while not current.is_dir() and current != current.parent:
            exact.add(current)
            current = current.parent
        if current.is_dir():
            directories.add(current)
    return directories, exact


def build_source_watch_spec(
    providers: Sequence[Mapping[str, object]],
    authorities: Mapping[str, CatalogAuthority],
) -> SourceWatchSpec:
    watch_roots, exact = _search_paths()
    identity_directories: set[Path] = set()
    for provider in providers:
        provider_id = str(provider.get("id") or "")
        authority = authorities.get(provider_id)
        if authority is not None:
            exact.update(_identity_paths(authority))
            identity_directories.update(_identity_directories(authority))
        config_root = _provider_config_root(provider)
        if config_root is not None:
            identity_directories.add(config_root)
        config_paths = _provider_config_paths(provider)
        exact.update(config_paths)
        for path in config_paths:
            root = _nearest_existing_directory(path)
            if root is not None:
                watch_roots.add(root)
    return SourceWatchSpec.build(
        exact_paths=exact,
        identity_directories=identity_directories,
        search_directories=watch_roots,
        search_names=(
            _WINDOWS_CODEX_NAMES if os.name == "nt" else ("codex",)
        ),
    )
