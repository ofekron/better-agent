from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from codex_execution_common import ExecutionContractError, SHA256_RE


MANIFEST_NAME = "execution_payload_manifest.json"
MANIFEST_VERSION = 1
PAYLOAD_ROOTS = frozenset({"claude-cli", "provider-cli"})
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def payload_file_mode() -> int:
    return 0o555 if os.name == "nt" else 0o500


@dataclass(frozen=True)
class PayloadFile:
    name: str
    kind: str
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ctime_ns": self.ctime_ns,
            "device": self.device,
            "inode": self.inode,
            "kind": self.kind,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
            "name": self.name,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class ExecutionPayloadManifest:
    run_id: str
    run_directory_device: int
    run_directory_inode: int
    payload_root: str
    files: tuple[PayloadFile, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": [item.as_dict() for item in self.files],
            "payload_root": self.payload_root,
            "run_directory": {
                "device": self.run_directory_device,
                "inode": self.run_directory_inode,
            },
            "run_id": self.run_id,
            "version": MANIFEST_VERSION,
        }


def canonical_bytes(manifest: ExecutionPayloadManifest) -> bytes:
    return (
        json.dumps(
            manifest.as_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _fail(message: str) -> None:
    raise ExecutionContractError(message)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("payload manifest contains duplicate keys")
        result[key] = value
    return result


def _required_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        _fail("payload manifest integer is invalid")
    return value


def parse_manifest(encoded: bytes) -> ExecutionPayloadManifest:
    try:
        raw = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionContractError(
            "payload manifest encoding is invalid",
        ) from exc
    if type(raw) is not dict or set(raw) != {
        "files",
        "payload_root",
        "run_directory",
        "run_id",
        "version",
    }:
        _fail("payload manifest schema is invalid")
    if type(raw["version"]) is not int or raw["version"] != MANIFEST_VERSION:
        _fail("payload manifest version is invalid")
    run_directory = raw["run_directory"]
    if type(run_directory) is not dict or set(run_directory) != {
        "device",
        "inode",
    }:
        _fail("payload manifest run directory is invalid")
    run_id = raw["run_id"]
    payload_root = raw["payload_root"]
    files_raw = raw["files"]
    if (
        type(run_id) is not str
        or not SAFE_NAME.fullmatch(run_id)
        or type(payload_root) is not str
        or payload_root not in PAYLOAD_ROOTS
        or type(files_raw) is not list
        or not files_raw
    ):
        _fail("payload manifest schema is invalid")
    files = tuple(_parse_file(item) for item in files_raw)
    if [item.name for item in files] != sorted(item.name for item in files):
        _fail("payload manifest files are not canonical")
    if len({item.name for item in files}) != len(files):
        _fail("payload manifest contains duplicate files")
    manifest = ExecutionPayloadManifest(
        run_id=run_id,
        run_directory_device=_required_int(run_directory["device"]),
        run_directory_inode=_required_int(run_directory["inode"]),
        payload_root=payload_root,
        files=files,
    )
    if canonical_bytes(manifest) != encoded:
        _fail("payload manifest encoding is not canonical")
    return manifest


def _parse_file(raw: Any) -> PayloadFile:
    if type(raw) is not dict or set(raw) != {
        "ctime_ns",
        "device",
        "inode",
        "kind",
        "mode",
        "mtime_ns",
        "name",
        "sha256",
        "size",
    }:
        _fail("payload manifest file schema is invalid")
    name = raw["name"]
    digest = raw["sha256"]
    if (
        type(name) is not str
        or not SAFE_NAME.fullmatch(name)
        or type(digest) is not str
        or not SHA256_RE.fullmatch(digest)
        or raw["kind"] != "regular_file"
        or type(raw["mode"]) is not int
        or raw["mode"] != payload_file_mode()
    ):
        _fail("payload manifest file is invalid")
    return PayloadFile(
        name=name,
        kind="regular_file",
        mode=payload_file_mode(),
        size=_required_int(raw["size"]),
        mtime_ns=_required_int(raw["mtime_ns"]),
        ctime_ns=_required_int(raw["ctime_ns"]),
        device=_required_int(raw["device"]),
        inode=_required_int(raw["inode"]),
        sha256=digest,
    )
