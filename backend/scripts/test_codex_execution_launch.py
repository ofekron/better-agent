from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_execution import (  # noqa: E402
    ExecutionContractError,
    resolve_codex_launch_chain,
)
from codex_execution_identity import FileIdentity  # noqa: E402
import codex_execution_launch as execution_launch  # noqa: E402
from codex_execution_launch import pinned_launch  # noqa: E402
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


def test_identity_capture_ignores_access_time_updates() -> None:
    with tempfile.TemporaryDirectory() as raw:
        executable = Path(raw) / "codex"
        write_executable(executable, b"native")
        current = executable.stat()
        os.utime(executable, ns=(0, current.st_mtime_ns))
        before = executable.stat()

        identity = FileIdentity.capture(executable)

        assert identity.sha256
        assert executable.stat().st_atime_ns != before.st_atime_ns


def test_shebang_resolution_ignores_access_time_updates() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        node = root / "runtime" / "node"
        launcher = root / "bin" / "codex"
        write_executable(node, b"node-runtime")
        write_executable(launcher, b"#!/usr/bin/env node\n")
        real_fstat = execution_launch.os.fstat
        calls = 0

        def changing_atime(descriptor: int) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            observed = real_fstat(descriptor)
            return SimpleNamespace(
                st_mode=observed.st_mode,
                st_size=observed.st_size,
                st_mtime_ns=observed.st_mtime_ns,
                st_ctime_ns=observed.st_ctime_ns,
                st_dev=observed.st_dev,
                st_ino=observed.st_ino,
                st_atime_ns=observed.st_atime_ns + calls,
            )

        execution_launch.os.fstat = changing_atime
        try:
            chain = resolve_codex_launch_chain(
                str(launcher),
                search_path=str(node.parent),
                platform="linux",
            )
        finally:
            execution_launch.os.fstat = real_fstat

        assert chain.attest()


def test_pinned_launch_executes_original_after_path_replacement() -> None:
    if os.name == "nt":
        return
    executable_source = Path(sys.executable).resolve()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        executable = root / "codex"
        os.link(executable_source, executable)
        chain = resolve_codex_launch_chain(str(executable))

        with pinned_launch(chain) as pinned:
            replacement = root / "replacement"
            replacement.write_bytes(b"not-the-original")
            replacement.chmod(replacement.stat().st_mode | 0o100)
            replacement.replace(executable)
            completed = subprocess.run(
                [
                    *pinned.argv_prefix,
                    "-c",
                    "print('original', end='')",
                ],
                capture_output=True,
                check=False,
                pass_fds=pinned.pass_fds,
                text=True,
            )

        assert completed.returncode == 0
        assert completed.stdout == "original"
        assert not chain.attest_metadata()


def test_pinned_launch_stages_code_mode_host_sibling() -> None:
    if os.name == "nt":
        return
    executable_source = Path(sys.executable).resolve()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        executable = root / "codex"
        sibling = root / "codex-code-mode-host"
        os.link(executable_source, executable)
        write_executable(sibling, b"code-mode-host-binary")
        chain = resolve_codex_launch_chain(str(executable))

        assert len(chain.sibling_components) == 1
        assert (
            Path(chain.sibling_components[0].resolved_path).name
            == "codex-code-mode-host"
        )

        with pinned_launch(chain) as pinned:
            staged_dir = Path(pinned.argv_prefix[0]).parent
            staged_sibling = staged_dir / "codex-code-mode-host"
            assert staged_sibling.is_file()
            assert staged_sibling.read_bytes() == b"code-mode-host-binary"


def test_resolve_launch_chain_without_sibling_leaves_it_empty() -> None:
    executable_source = Path(sys.executable).resolve()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        executable = root / "codex"
        os.link(executable_source, executable)
        chain = resolve_codex_launch_chain(str(executable))

        assert chain.sibling_components == ()


def test_windows_pinned_launch_denies_component_replacement() -> None:
    if os.name != "nt":
        return
    executable_source = Path(sys.executable).resolve()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        executable = root / "codex.exe"
        os.link(executable_source, executable)
        chain = resolve_codex_launch_chain(str(executable), platform="win32")

        with pinned_launch(chain) as pinned:
            replacement = root / "replacement.exe"
            os.link(executable_source, replacement)
            try:
                replacement.replace(executable)
            except PermissionError:
                pass
            else:
                raise AssertionError("Windows launch component was replaceable")
            completed = subprocess.run(
                [
                    *pinned.argv_prefix,
                    "-c",
                    "print('original', end='')",
                ],
                capture_output=True,
                check=False,
                text=True,
            )

        assert completed.returncode == 0
        assert completed.stdout == "original"


LAUNCH_TESTS = (
    test_native_symlink_chain_is_attested_and_retarget_fails,
    test_same_size_same_mtime_replacement_fails_strong_identity,
    test_env_shebang_binds_interpreter_and_script,
    test_windows_cmd_prefers_single_vendor_binary,
    test_x64_vendor_package_suffix_is_resolved,
    test_ambiguous_vendor_targets_fail_closed,
    test_open_attested_components_pins_exact_bytes,
    test_identity_capture_ignores_access_time_updates,
    test_shebang_resolution_ignores_access_time_updates,
    test_pinned_launch_executes_original_after_path_replacement,
    test_pinned_launch_stages_code_mode_host_sibling,
    test_resolve_launch_chain_without_sibling_leaves_it_empty,
    test_windows_pinned_launch_denies_component_replacement,
)


if __name__ == "__main__":
    for test in LAUNCH_TESTS:
        test()
    print("PASS codex execution launch")
