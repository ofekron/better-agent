from __future__ import annotations

import sys
from pathlib import Path

import extension_store


class ExtensionPackageUnavailable(RuntimeError):
    pass


def package_root(extension_id: str) -> Path:
    clean_extension_id = str(extension_id or "").strip()
    if not clean_extension_id:
        raise ExtensionPackageUnavailable("extension_id is required")
    record = extension_store.get_extension(clean_extension_id)
    if not record or not extension_store.is_extension_runtime_ready(clean_extension_id):
        raise ExtensionPackageUnavailable("extension is not active")
    source = record.get("source") or {}
    install_path = Path(str(source.get("install_path") or "")).expanduser()
    if not install_path.is_dir():
        raise ExtensionPackageUnavailable("extension package is unavailable")
    return install_path.resolve()


def ensure_package_importable(extension_id: str, package_name: str) -> Path:
    clean_package_name = str(package_name or "").strip()
    if not clean_package_name or "/" in clean_package_name or "\\" in clean_package_name:
        raise ExtensionPackageUnavailable("package_name must be a top-level package")
    root = package_root(extension_id)
    package_dir = root / clean_package_name
    if not package_dir.is_dir():
        raise ExtensionPackageUnavailable(f"extension package is missing {clean_package_name}")
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return root


def prompt_path(extension_id: str, name: str) -> Path | None:
    try:
        root = package_root(extension_id)
    except ExtensionPackageUnavailable:
        return None
    for base in (root / "prompts", root / "provisioning" / "prompts"):
        path = (base / name).resolve()
        try:
            path.relative_to(base.resolve())
        except ValueError:
            continue
        if path.is_file():
            return path
    return None
