from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from codex_execution_common import ExecutionContractError  # noqa: E402
from codex_execution_identity import FileIdentity  # noqa: E402
from provider_family_launch_attestation import (  # noqa: E402
    capture_cli_launch,
)
from provider_pinned_launch import (  # noqa: E402
    materialize_sdk_launch_cached,
    sdk_launch_cache_root,
)
import run_execution_payloads  # noqa: E402
from paths import make_private_directory, make_private_file  # noqa: E402
from run_execution_payloads import (  # noqa: E402
    MANIFEST_NAME,
    publish_execution_payload_manifest,
    read_execution_payload_manifest,
)


def _expect_rejected(operation) -> None:
    try:
        operation()
    except ExecutionContractError:
        return
    raise AssertionError("unsafe payload manifest operation was accepted")


def _write_payload(path: Path, data: bytes) -> None:
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(data)
    make_private_file(path)
    path.chmod(stat.S_IREAD if os.name == "nt" else 0o500)


def _unlink_readonly(path: Path) -> None:
    if os.name == "nt":
        path.chmod(stat.S_IWRITE)
    path.unlink()


def _cache_payload(tag: str) -> Path:
    fingerprint = hashlib.sha256(tag.encode("utf-8")).hexdigest()
    entry = sdk_launch_cache_root() / fingerprint
    entry.mkdir(mode=0o700, exist_ok=True)
    make_private_directory(entry)
    payload = entry / "root"
    payload.mkdir(mode=0o700, exist_ok=True)
    make_private_directory(payload)
    return payload


def _run_dir(root: Path, run_id: str) -> Path:
    runs = root / "runs"
    runs.mkdir(mode=0o700, parents=True, exist_ok=True)
    make_private_directory(runs)
    run_dir = runs / run_id
    run_dir.mkdir(mode=0o700, exist_ok=True)
    make_private_directory(run_dir)
    return run_dir


def _run(root: Path, run_id: str):
    run_dir = _run_dir(root, run_id)
    payload = _cache_payload(run_id)
    executable = payload / (
        "provider-cli.exe" if os.name == "nt" else "provider-cli"
    )
    _write_payload(executable, b"#!/bin/sh\nexit 0\n")
    return run_dir, payload, executable, FileIdentity.capture(executable)


def _canonical_write(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    make_private_file(path)
    path.chmod(stat.S_IREAD if os.name == "nt" else 0o400)


def _round_trip_and_idempotency(root: Path) -> None:
    for index, payload_root in enumerate(("claude-cli", "provider-cli")):
        run_dir = _run_dir(root, f"round-trip-{index}")
        source = Path(sys.executable)
        if os.name != "nt":
            source = root / f"source-{index}"
            source.write_bytes(b"#!/bin/sh\nexit 0\n")
            source.chmod(0o700)
        launch = capture_cli_launch(
            logical_command="provider",
            launcher_path=source,
            platform=sys.platform,
        )
        materialized = materialize_sdk_launch_cached(launch)
        executable = Path(materialized.executable_path)
        assert executable.is_relative_to(sdk_launch_cache_root())
        written = publish_execution_payload_manifest(
            run_dir,
            payload_root,
            materialized.payload_files,
        )
        assert read_execution_payload_manifest(run_dir) == written
        assert publish_execution_payload_manifest(
            run_dir,
            payload_root,
            materialized.payload_files,
        ) == written
        assert materialized.attest()
        manifest = run_dir / MANIFEST_NAME
        expected_mode = 0o444 if os.name == "nt" else 0o400
        assert stat.S_IMODE(manifest.stat().st_mode) == expected_mode
        assert manifest.stat().st_nlink == 1
        try:
            manifest.write_bytes(b"replacement")
        except PermissionError:
            pass
        else:
            raise AssertionError("published payload manifest remained writable")
        assert written.payload_root_fingerprint == (
            executable.parent.parent.name
        )
        assert written.files[0].name == executable.name
        assert "/" not in written.files[0].name


def _concurrent_publication(root: Path) -> None:
    run_dir, _, _, identity = _run(root, "concurrent")
    results: list[object] = []

    def publish() -> None:
        try:
            results.append(
                publish_execution_payload_manifest(
                    run_dir,
                    "provider-cli",
                    (identity,),
                )
            )
        except ExecutionContractError as exc:
            results.append(exc)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    authoritative = read_execution_payload_manifest(run_dir)
    assert all(
        result == authoritative or isinstance(result, ExecutionContractError)
        for result in results
    )
    assert (run_dir / MANIFEST_NAME).stat().st_nlink == 1


def _closed_root_and_identity_rejections(root: Path) -> None:
    run_dir, payload, executable, identity = _run(root, "closed-root")
    outside = run_dir / ("outside.exe" if os.name == "nt" else "outside")
    _write_payload(outside, executable.read_bytes())
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "provider-cli",
            (FileIdentity.capture(outside),),
        )
    )
    run_local_root = run_dir / "provider-cli"
    run_local_root.mkdir(mode=0o700)
    make_private_directory(run_local_root)
    run_local = run_local_root / (
        "provider-cli.exe" if os.name == "nt" else "provider-cli"
    )
    _write_payload(run_local, executable.read_bytes())
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "provider-cli",
            (FileIdentity.capture(run_local),),
        )
    )
    fingerprint_level = payload.parent / (
        "fingerprint-level.exe" if os.name == "nt" else "fingerprint-level"
    )
    _write_payload(fingerprint_level, executable.read_bytes())
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "provider-cli",
            (FileIdentity.capture(fingerprint_level),),
        )
    )
    non_hex_root = sdk_launch_cache_root() / "not-a-fingerprint" / "root"
    non_hex_root.mkdir(mode=0o700, parents=True)
    make_private_directory(non_hex_root.parent)
    make_private_directory(non_hex_root)
    non_hex = non_hex_root / (
        "provider-cli.exe" if os.name == "nt" else "provider-cli"
    )
    _write_payload(non_hex, executable.read_bytes())
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "provider-cli",
            (FileIdentity.capture(non_hex),),
        )
    )
    other_entry = _cache_payload("closed-root-second-entry")
    other_file = other_entry / (
        "provider-runtime.exe" if os.name == "nt" else "provider-runtime"
    )
    _write_payload(other_file, executable.read_bytes())
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "provider-cli",
            (identity, FileIdentity.capture(other_file)),
        )
    )
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "arbitrary-root",
            (identity,),
        )
    )
    hardlink = payload / "hardlink"
    os.link(executable, hardlink)
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "provider-cli",
            (identity,),
        )
    )
    if os.name == "nt":
        hardlink.chmod(stat.S_IWRITE)
    hardlink.unlink()
    symlink = payload / "symlink"
    symlink.symlink_to(executable)
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "provider-cli",
            (FileIdentity.capture(symlink),),
        )
    )


def _different_publication_rejected(root: Path) -> None:
    run_dir, payload, _, identity = _run(root, "different-publication")
    publish_execution_payload_manifest(
        run_dir,
        "provider-cli",
        (identity,),
    )
    alternative = payload / (
        "provider-runtime.exe" if os.name == "nt" else "provider-runtime"
    )
    _write_payload(alternative, b"other")
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "provider-cli",
            (FileIdentity.capture(alternative),),
        )
    )
    claude_payload = _cache_payload("different-publication-claude")
    claude_file = claude_payload / (
        "provider-cli.exe" if os.name == "nt" else "provider-cli"
    )
    _write_payload(claude_file, b"other")
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "claude-cli",
            (FileIdentity.capture(claude_file),),
        )
    )


def _immutable_and_strict_reader(root: Path) -> None:
    run_dir, _, executable, identity = _run(root, "immutable")
    publish_execution_payload_manifest(
        run_dir,
        "provider-cli",
        (identity,),
    )
    _write_payload(executable, b"tampered")
    _expect_rejected(lambda: read_execution_payload_manifest(run_dir))

    strict_dir, _, _, strict_identity = _run(root, "strict-reader")
    manifest = publish_execution_payload_manifest(
        strict_dir,
        "provider-cli",
        (strict_identity,),
    ).as_dict()
    manifest_path = strict_dir / MANIFEST_NAME
    _unlink_readonly(manifest_path)
    duplicate = (
        '{"files":[],"files":[],"payload_root":"provider-cli",'
        '"payload_root_fingerprint":"' + "0" * 64 + '",'
        '"run_directory":{"device":1,"inode":1},'
        '"run_id":"strict-reader","version":2}\n'
    )
    manifest_path.write_text(duplicate, encoding="utf-8")
    make_private_file(manifest_path)
    manifest_path.chmod(stat.S_IREAD if os.name == "nt" else 0o400)
    _expect_rejected(lambda: read_execution_payload_manifest(strict_dir))
    _unlink_readonly(manifest_path)
    manifest["unexpected"] = True
    _canonical_write(manifest_path, manifest)
    _expect_rejected(lambda: read_execution_payload_manifest(strict_dir))


def _manifest_path_swap_rejected(root: Path) -> None:
    run_dir, _, _, identity = _run(root, "swap-race")
    publish_execution_payload_manifest(
        run_dir,
        "provider-cli",
        (identity,),
    )
    manifest_path = run_dir / MANIFEST_NAME
    replacement = run_dir / "replacement"
    replacement.write_bytes(manifest_path.read_bytes())
    make_private_file(replacement)
    replacement.chmod(stat.S_IREAD if os.name == "nt" else 0o400)
    original_read = os.read
    swapped = False
    swap_denied = False

    def swap_after_read(descriptor: int, size: int) -> bytes:
        nonlocal swap_denied, swapped
        chunk = original_read(descriptor, size)
        if chunk and not swapped:
            try:
                manifest_path.rename(run_dir / "displaced-manifest")
                replacement.rename(manifest_path)
                swapped = True
            except PermissionError:
                if os.name != "nt":
                    raise
                swap_denied = True
        return chunk

    with patch.object(run_execution_payloads.os, "read", swap_after_read):
        if os.name == "nt":
            read_execution_payload_manifest(run_dir)
        else:
            _expect_rejected(lambda: read_execution_payload_manifest(run_dir))
    assert swapped or swap_denied


def _payload_path_swap_rejected(root: Path) -> None:
    run_dir, payload, executable, identity = _run(root, "payload-swap-race")
    publish_execution_payload_manifest(
        run_dir,
        "provider-cli",
        (identity,),
    )
    replacement = payload / (
        "replacement.exe" if os.name == "nt" else "replacement"
    )
    _write_payload(replacement, executable.read_bytes())
    displaced = payload / (
        "displaced.exe" if os.name == "nt" else "displaced"
    )
    original_capture = FileIdentity.capture
    swapped = False

    def swap_then_capture(path: Path):
        nonlocal swapped
        if not swapped:
            executable.rename(displaced)
            replacement.rename(executable)
            swapped = True
        return original_capture(path)

    with patch.object(FileIdentity, "capture", side_effect=swap_then_capture):
        _expect_rejected(lambda: read_execution_payload_manifest(run_dir))
    assert swapped


def _retry_hashes_large_payload_once(root: Path) -> None:
    run_dir = _run_dir(root, "large-retry")
    payload = _cache_payload("large-retry")
    executable = payload / (
        "provider-cli.exe" if os.name == "nt" else "provider-cli"
    )
    executable.touch()
    os.truncate(executable, 257 * 1024 * 1024)
    make_private_file(executable)
    executable.chmod(stat.S_IREAD if os.name == "nt" else 0o500)
    identity = FileIdentity.capture(executable)
    publish_execution_payload_manifest(
        run_dir,
        "provider-cli",
        (identity,),
    )
    original_capture = FileIdentity.capture
    with patch.object(
        FileIdentity,
        "capture",
        wraps=original_capture,
    ) as capture:
        publish_execution_payload_manifest(
            run_dir,
            "provider-cli",
            (identity,),
        )
    assert capture.call_count == 1


def _windows_directory_flush_failure_rejected(root: Path) -> None:
    run_dir, _, _, identity = _run(root, "windows-flush-failure")
    with (
        patch.object(run_execution_payloads, "_is_windows", return_value=True),
        patch.object(
            run_execution_payloads,
            "_flush_windows_directory",
            side_effect=OSError("flush failed"),
        ),
    ):
        _expect_rejected(
            lambda: publish_execution_payload_manifest(
                run_dir,
                "provider-cli",
                (identity,),
            )
        )


def _windows_acl_and_reparse_integration(root: Path) -> None:
    class ReparseIdentity:
        st_file_attributes = 0x400

    assert run_execution_payloads._is_windows_reparse_point(
        ReparseIdentity(),
    )
    if os.name != "nt":
        return

    run_dir, _, _, identity = _run(root, "insecure-acl")
    subprocess.run(
        [
            "icacls",
            str(run_dir),
            "/grant",
            "*S-1-1-0:(OI)(CI)F",
        ],
        check=True,
        capture_output=True,
    )
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            run_dir,
            "provider-cli",
            (identity,),
        )
    )

    runs = root / "runs"
    target = runs / "junction-target"
    target.mkdir()
    make_private_directory(target)
    target_payload = _cache_payload("junction-target")
    target_executable = target_payload / (
        "provider-cli.exe" if os.name == "nt" else "provider-cli"
    )
    _write_payload(target_executable, b"junction")
    target_identity = FileIdentity.capture(target_executable)
    junction = runs / "junction-run"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
    )
    _expect_rejected(
        lambda: publish_execution_payload_manifest(
            junction,
            "provider-cli",
            (target_identity,),
        )
    )


def _replacement_run_rejected(root: Path) -> None:
    original, _, _, identity = _run(root, "replacement")
    publish_execution_payload_manifest(
        original,
        "provider-cli",
        (identity,),
    )
    manifest_bytes = (original / MANIFEST_NAME).read_bytes()
    displaced = root / "displaced"
    original.rename(displaced)
    replacement, _, executable, _ = _run(root, "replacement")
    (replacement / MANIFEST_NAME).write_bytes(manifest_bytes)
    make_private_file(replacement / MANIFEST_NAME)
    (replacement / MANIFEST_NAME).chmod(
        stat.S_IREAD if os.name == "nt" else 0o400,
    )
    executable.chmod(0o500)
    _expect_rejected(lambda: read_execution_payload_manifest(replacement))


def _missing_manifest_rejected(root: Path) -> None:
    run_dir, _, _, _ = _run(root, "missing")
    _expect_rejected(lambda: read_execution_payload_manifest(run_dir))


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        os.environ["BETTER_AGENT_HOME"] = str(root)
        _round_trip_and_idempotency(root)
        _concurrent_publication(root)
        _closed_root_and_identity_rejections(root)
        _different_publication_rejected(root)
        _immutable_and_strict_reader(root)
        _manifest_path_swap_rejected(root)
        _payload_path_swap_rejected(root)
        _retry_hashes_large_payload_once(root)
        _windows_directory_flush_failure_rejected(root)
        _windows_acl_and_reparse_integration(root)
        _replacement_run_rejected(root)
        _missing_manifest_rejected(root)
    print("run execution payload manifest tests passed")


if __name__ == "__main__":
    main()
