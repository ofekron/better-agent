from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_execution import (  # noqa: E402
    ExecutionContractError,
    resolve_codex_launch_chain,
)
from scripts.codex_execution_test_support import write_executable  # noqa: E402


def test_native_symlink_chain_is_attested_and_retarget_fails() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        first = root / "release-a" / "codex"
        second = root / "release-b" / "codex"
        write_executable(first, b"native-a")
        write_executable(second, b"native-b")
        launcher = root / "bin" / "codex"
        launcher.parent.mkdir()
        launcher.symlink_to(first)

        chain = resolve_codex_launch_chain(str(launcher))

        assert chain.argv_prefix == (str(first.resolve()),)
        assert chain.attest()
        launcher.unlink()
        launcher.symlink_to(second)
        assert not chain.attest()


def test_same_size_same_mtime_replacement_fails_strong_identity() -> None:
    with tempfile.TemporaryDirectory() as raw:
        executable = Path(raw) / "codex"
        write_executable(executable, b"aaaa")
        original = executable.stat()
        chain = resolve_codex_launch_chain(str(executable))

        executable.write_bytes(b"bbbb")
        os.utime(
            executable,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )

        assert executable.stat().st_size == original.st_size
        assert executable.stat().st_mtime_ns == original.st_mtime_ns
        assert not chain.attest()


def test_env_shebang_binds_interpreter_and_script() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        node = root / "runtime" / "node"
        script = root / "bin" / "codex"
        write_executable(node, b"node-runtime")
        write_executable(script, b"#!/usr/bin/env node\nconsole.log('codex')\n")

        chain = resolve_codex_launch_chain(
            str(script),
            search_path=str(node.parent),
            platform="linux",
        )

        assert chain.argv_prefix == (str(node.resolve()), str(script.resolve()))
        assert chain.attest()
        node.write_bytes(b"node-mutated")
        assert not chain.attest()


def test_windows_cmd_prefers_single_vendor_binary() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        launcher = root / "codex.cmd"
        write_executable(launcher, b"@echo off\r\n")
        vendor = (
            root
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-arm64"
            / "vendor"
            / "aarch64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        write_executable(vendor, b"windows-native")

        chain = resolve_codex_launch_chain(
            str(launcher),
            platform="win32",
        )

        assert chain.argv_prefix == (str(vendor.resolve()),)
        assert chain.attest()


def test_x64_vendor_package_suffix_is_resolved() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        launcher = root / "codex.cmd"
        write_executable(launcher, b"@echo off\r\n")
        vendor = (
            root
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        write_executable(vendor, b"windows-x64")

        chain = resolve_codex_launch_chain(
            str(launcher),
            platform="win32",
            architecture="AMD64",
        )

        assert chain.argv_prefix == (str(vendor.resolve()),)


def test_ambiguous_vendor_targets_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        launcher = root / "codex.cmd"
        write_executable(launcher, b"@echo off\r\n")
        for variant in ("first", "second"):
            vendor = (
                root
                / "node_modules"
                / "@openai"
                / "codex"
                / "node_modules"
                / "@openai"
                / "codex-win32-arm64"
                / "vendor"
                / variant
                / "bin"
                / "codex.exe"
            )
            write_executable(vendor, variant.encode())

        try:
            resolve_codex_launch_chain(str(launcher), platform="win32")
        except ExecutionContractError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("ambiguous vendor targets must fail closed")


def test_open_attested_components_pins_exact_bytes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        executable = root / "codex"
        write_executable(executable, b"native-a")
        chain = resolve_codex_launch_chain(str(executable))

        with chain.open_attested_components() as handles:
            replacement = root / "replacement"
            write_executable(replacement, b"native-b")
            replacement.replace(executable)
            os.lseek(handles[0], 0, os.SEEK_SET)
            pinned = os.read(handles[0], 64)

        assert pinned == b"native-a"
        assert not chain.attest_metadata()


LAUNCH_TESTS = (
    test_native_symlink_chain_is_attested_and_retarget_fails,
    test_same_size_same_mtime_replacement_fails_strong_identity,
    test_env_shebang_binds_interpreter_and_script,
    test_windows_cmd_prefers_single_vendor_binary,
    test_x64_vendor_package_suffix_is_resolved,
    test_ambiguous_vendor_targets_fail_closed,
    test_open_attested_components_pins_exact_bytes,
)


if __name__ == "__main__":
    for test in LAUNCH_TESTS:
        test()
    print("PASS codex execution launch")
