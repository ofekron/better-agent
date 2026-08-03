from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codex_execution_launch as launch  # noqa: E402
from codex_execution_common import (  # noqa: E402
    ExecutionContractError,
)
from codex_execution_identity import FileIdentity  # noqa: E402
from codex_execution_launch import (  # noqa: E402
    LaunchChain,
    _open_attested,
    _read_shebang_tokens,
    _shebang_prefix,
    _trusted_system_interpreter,
    _verify_component_handle,
    _vendor_candidates,
    pinned_launch,
    resolve_codex_launch_chain,
    validate_launch_chain,
)
from scripts.codex_execution_test_support import write_executable  # noqa: E402

_SHA = "a" * 64


def _clear_hash_cache() -> None:
    from codex_execution_common import _hash_cache, _hash_cache_lock

    with _hash_cache_lock:
        _hash_cache.clear()


def _ident(requested: str = "/x/codex", resolved: str = "/x/codex") -> FileIdentity:
    return FileIdentity(
        requested_path=requested,
        resolved_path=resolved,
        sha256=_SHA,
        size=1,
        mtime_ns=1,
        ctime_ns=1,
        device=1,
        inode=1,
    )


def _native_chain(**overrides) -> LaunchChain:
    base = dict(
        logical_command="codex",
        mode="native",
        launcher=_ident(),
        argv_prefix=("/x/codex",),
        components=(_ident(),),
        component_argv_indexes=(0,),
    )
    base.update(overrides)
    return LaunchChain(**base)


# ---------------------------------------------------------------------------
# _open_attested: identity mismatch during open
# ---------------------------------------------------------------------------


def test_open_attested_rejects_swapped_component() -> None:
    # Line 62: a component swapped between capture and open -> mismatch raise.
    with tempfile.TemporaryDirectory() as raw:
        executable = Path(raw) / "codex"
        write_executable(executable, b"original")
        chain = resolve_codex_launch_chain(str(executable))
        # Replace the file underneath so the open-time identity check fails.
        executable.write_bytes(b"different-content-now")
        os.chmod(executable, executable.stat().st_mode | 0o111)
        with pytest.raises(ExecutionContractError, match="identity mismatch"):
            with chain.open_attested_components():
                pass


def test_open_attested_empty_components_yields_empty() -> None:
    with _open_attested(()) as handles:
        assert handles == ()


# ---------------------------------------------------------------------------
# _trusted_system_interpreter
# ---------------------------------------------------------------------------


def test_trusted_system_interpreter_true_for_root_owned_bin_binary() -> None:
    # /bin/sh is root-owned, mode 755, under /bin -> trusted.
    sh = Path("/bin/sh")
    if not sh.is_file():
        pytest.skip("/bin/sh unavailable on this platform")
    identity = FileIdentity.capture(sh)
    fd = os.open(str(sh), os.O_RDONLY)
    try:
        assert _trusted_system_interpreter(fd, identity) is True
    finally:
        os.close(fd)


def test_trusted_system_interpreter_false_for_user_owned_file() -> None:
    # A tmpdir file is not uid-0 owned and not under /bin -> not trusted.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "interp"
        write_executable(p, b"interp")
        identity = FileIdentity.capture(p)
        fd = os.open(str(p), os.O_RDONLY)
        try:
            assert _trusted_system_interpreter(fd, identity) is False
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# _verify_component_handle (exercised directly; also the Windows open path)
# ---------------------------------------------------------------------------


def test_verify_component_handle_match_and_mismatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "comp"
        write_executable(p, b"component")
        identity = FileIdentity.capture(p)
        fd = os.open(str(p), os.O_RDONLY)
        try:
            _verify_component_handle(fd, identity)  # match -> no raise

            wrong = FileIdentity(
                requested_path=identity.requested_path,
                resolved_path=identity.resolved_path,
                sha256="0" * 64,
                size=identity.size,
                mtime_ns=identity.mtime_ns,
                ctime_ns=identity.ctime_ns,
                device=identity.device,
                inode=identity.inode,
            )
            with pytest.raises(ExecutionContractError, match="identity mismatch"):
                _verify_component_handle(fd, wrong)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# pinned_launch: shebang interpreter that is not a trusted system binary
# ---------------------------------------------------------------------------


def test_pinned_launch_rejects_untrusted_shebang_interpreter() -> None:
    # Lines 226-233: shebang mode, interpreter not under /bin or /usr/bin ->
    # _trusted_system_interpreter is False -> the interpreter cannot be pinned.
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        node = root / "runtime" / "node"
        script = root / "bin" / "codex"
        write_executable(node, b"node-runtime")
        write_executable(script, b"#!/usr/bin/env node\nconsole.log('x')\n")
        chain = resolve_codex_launch_chain(
            str(script), search_path=str(node.parent), platform="linux"
        )
        assert chain.mode == "shebang"
        with pytest.raises(ExecutionContractError, match="interpreter cannot be pinned"):
            with pinned_launch(chain):
                pass


def test_pinned_launch_proc_self_fd_fast_path() -> None:
    # Lines 212-216: Linux /proc/self/fd fast path (no sibling, /proc/self/fd
    # exists). Only meaningful on Linux; skip elsewhere.
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("/proc/self/fd is specific to Linux")
    with tempfile.TemporaryDirectory() as raw:
        executable = Path(raw) / "codex"
        write_executable(executable, b"native")
        chain = resolve_codex_launch_chain(str(executable))
        assert chain.sibling_components == ()
        with pinned_launch(chain) as pinned:
            assert pinned.argv_prefix[0].startswith("/proc/self/fd/")
            assert pinned.pass_fds


def test_pinned_launch_stages_trusted_bin_sh_shebang() -> None:
    # Lines 233 + 331: a shebang whose interpreter is a trusted /bin binary
    # takes the `continue` branch (no interpreter copy), and validate assembles
    # the non-env expected argv. Enter/exit only; no subprocess is launched.
    sh = Path("/bin/sh")
    if not sh.is_file():
        pytest.skip("/bin/sh unavailable on this platform")
    with tempfile.TemporaryDirectory() as raw:
        script = Path(raw) / "codex"
        write_executable(script, b"#!/bin/sh\necho hi\n")
        chain = resolve_codex_launch_chain(str(script))
        assert chain.mode == "shebang"
        with pinned_launch(chain) as pinned:
            # Interpreter stays /bin/sh; the script is staged to a temp dir.
            assert pinned.argv_prefix[0] == str(sh.resolve())
            staged = Path(pinned.argv_prefix[1])
            assert staged.is_file()
            assert staged.read_bytes().startswith(b"#!/bin/sh")


# ---------------------------------------------------------------------------
# validate_launch_chain: native happy path + every incoherence gate
# ---------------------------------------------------------------------------


def test_validate_native_chain_is_accepted() -> None:
    validate_launch_chain(_native_chain())  # no raise


def _expect_incoherent(chain: LaunchChain, match: str = "launch chain") -> None:
    with pytest.raises(ExecutionContractError, match=match):
        validate_launch_chain(chain)


def test_validate_rejects_wrong_logical_command() -> None:
    _expect_incoherent(_native_chain(logical_command="other"))


def test_validate_rejects_unknown_mode() -> None:
    _expect_incoherent(_native_chain(mode="weird"))


def test_validate_rejects_launcher_not_named_codex() -> None:
    launcher = _ident(requested="/x/release", resolved="/x/release")
    _expect_incoherent(_native_chain(launcher=launcher))


def test_validate_rejects_empty_argv_prefix() -> None:
    _expect_incoherent(_native_chain(argv_prefix=(), components=(), component_argv_indexes=()))


def test_validate_rejects_index_component_length_mismatch() -> None:
    _expect_incoherent(_native_chain(component_argv_indexes=(0, 1)))


def test_validate_rejects_duplicate_indexes() -> None:
    chain = LaunchChain(
        logical_command="codex",
        mode="native",
        launcher=_ident(),
        argv_prefix=("/x/codex", "/x/codex"),
        components=(_ident(), _ident()),
        component_argv_indexes=(0, 0),
    )
    _expect_incoherent(chain)


def test_validate_rejects_first_index_not_zero() -> None:
    _expect_incoherent(_native_chain(component_argv_indexes=(1,)))


def test_validate_rejects_index_out_of_range() -> None:
    _expect_incoherent(_native_chain(component_argv_indexes=(5,)))


def test_validate_rejects_argv_value_not_matching_component() -> None:
    # Line 278: argv_prefix[index] is neither requested nor resolved path.
    chain = LaunchChain(
        logical_command="codex",
        mode="native",
        launcher=_ident(),
        argv_prefix=("/totally/different",),
        components=(_ident(),),
        component_argv_indexes=(0,),
    )
    _expect_incoherent(chain)


def test_validate_rejects_control_chars_in_argv() -> None:
    # Line 281: NUL / newline in an argv value.
    for bad in ("/x/codex\x00", "/x/codex\n"):
        chain = LaunchChain(
            logical_command="codex",
            mode="native",
            launcher=_ident(),
            argv_prefix=(bad,),
            components=(_ident(requested=bad, resolved=bad),),
            component_argv_indexes=(0,),
        )
        _expect_incoherent(chain)


def test_validate_rejects_unattested_absolute_path() -> None:
    # Line 283: an absolute, non-indexed argv entry.
    chain = LaunchChain(
        logical_command="codex",
        mode="native",
        launcher=_ident(),
        argv_prefix=("/x/codex", "/unattested/path"),
        components=(_ident(),),
        component_argv_indexes=(0,),
    )
    with pytest.raises(ExecutionContractError, match="unattested"):
        validate_launch_chain(chain)


def test_validate_rejects_secret_in_argv() -> None:
    # Line 287: a secret-looking non-indexed argv value.
    chain = LaunchChain(
        logical_command="codex",
        mode="native",
        launcher=_ident(),
        argv_prefix=("/x/codex", "--api_key=leak"),
        components=(_ident(),),
        component_argv_indexes=(0,),
    )
    with pytest.raises(ExecutionContractError, match="secret"):
        validate_launch_chain(chain)


def test_validate_rejects_more_than_one_sibling() -> None:
    # Line 289.
    chain = LaunchChain(
        logical_command="codex",
        mode="native",
        launcher=_ident(),
        argv_prefix=("/x/codex",),
        components=(_ident(),),
        component_argv_indexes=(0,),
        sibling_components=(_ident(), _ident()),
    )
    _expect_incoherent(chain, match="siblings")


def test_validate_rejects_misnamed_sibling() -> None:
    # Line 298: sibling basename not codex-code-mode-host nor .exe.
    chain = LaunchChain(
        logical_command="codex",
        mode="native",
        launcher=_ident(),
        argv_prefix=("/x/codex",),
        components=(_ident(resolved="/dir/codex"),),
        component_argv_indexes=(0,),
        sibling_components=(_ident(requested="/dir/other", resolved="/dir/other"),),
    )
    _expect_incoherent(chain, match="siblings")


def test_validate_rejects_native_incoherence() -> None:
    # Line 305: native mode but argv_prefix != (resolved,). The component's
    # requested path still satisfies the argv-match gate, so execution reaches
    # the native-specific check.
    comp = _ident(requested="/x/codex", resolved="/x/other")
    chain = LaunchChain(
        logical_command="codex",
        mode="native",
        launcher=_ident(),
        argv_prefix=("/x/codex",),
        components=(comp,),
        component_argv_indexes=(0,),
    )
    _expect_incoherent(chain, match="native")


def _shebang_chain(tmp: Path, *, shebang: bytes, interpreter_path: Path,
                   argv_prefix: tuple[str, ...] | None = None,
                   components: tuple[FileIdentity, ...] | None = None,
                   script_name: str = "codex") -> LaunchChain:
    script = tmp / "bin" / script_name
    script.parent.mkdir(parents=True, exist_ok=True)
    write_executable(script, shebang)
    launcher = FileIdentity.capture(script)
    interp = FileIdentity.capture(interpreter_path)
    return LaunchChain(
        logical_command="codex",
        mode="shebang",
        launcher=launcher,
        argv_prefix=argv_prefix if argv_prefix is not None else (
            str(Path(interpreter_path).resolve()), str(script.resolve())
        ),
        components=components if components is not None else (interp, launcher),
        component_argv_indexes=(0, 1),
    )


def test_validate_rejects_shebang_wrong_component_count() -> None:
    # Line 308: shebang mode with != 2 components. This raise precedes the
    # shebang file read, so no real launcher file is required.
    chain = LaunchChain(
        logical_command="codex",
        mode="shebang",
        launcher=_ident(),
        argv_prefix=("/x/codex",),
        components=(_ident(),),
        component_argv_indexes=(0,),
    )
    _expect_incoherent(chain, match="shebang")


def test_validate_rejects_shebang_script_not_launcher() -> None:
    # Line 311: script.resolved_path != launcher.resolved_path.
    with tempfile.TemporaryDirectory() as raw:
        node = Path(raw) / "node"
        write_executable(node, b"node")
        other = Path(raw) / "other"
        write_executable(other, b"other")
        script = Path(raw) / "bin" / "codex"
        script.parent.mkdir(parents=True, exist_ok=True)
        write_executable(script, b"#!/usr/bin/env node\n")
        launcher = FileIdentity.capture(script)
        interp = FileIdentity.capture(node)
        other_ident = FileIdentity.capture(other)
        chain = LaunchChain(
            logical_command="codex",
            mode="shebang",
            launcher=launcher,
            argv_prefix=(str(node.resolve()), str(other.resolve())),
            components=(interp, other_ident),  # script != launcher
            component_argv_indexes=(0, 1),
        )
        _expect_incoherent(chain, match="shebang")


def test_validate_rejects_env_shebang_ambiguous() -> None:
    # Line 318: /usr/bin/env with a flag-like or multi-word target.
    with tempfile.TemporaryDirectory() as raw:
        node = Path(raw) / "node"
        write_executable(node, b"node")
        # Shebang names env with a leading-dash arg -> ambiguous.
        chain = _shebang_chain(
            Path(raw), shebang=b"#!/usr/bin/env -node\n", interpreter_path=node
        )
        with pytest.raises(ExecutionContractError, match="ambiguous"):
            validate_launch_chain(chain)


def test_validate_rejects_env_shebang_interpreter_mismatch() -> None:
    # Line 320: env target name != interpreter basename.
    with tempfile.TemporaryDirectory() as raw:
        node = Path(raw) / "node"
        write_executable(node, b"node")
        chain = _shebang_chain(
            Path(raw), shebang=b"#!/usr/bin/env ruby\n", interpreter_path=node
        )
        _expect_incoherent(chain, match="shebang interpreter")


def test_validate_rejects_absolute_shebang_interpreter_unavailable() -> None:
    # Lines 323-328: non-env interpreter path that does not resolve.
    with tempfile.TemporaryDirectory() as raw:
        node = Path(raw) / "node"
        write_executable(node, b"node")
        script = Path(raw) / "bin" / "codex"
        script.parent.mkdir(parents=True, exist_ok=True)
        write_executable(script, b"#!/no/such/interp\n")
        launcher = FileIdentity.capture(script)
        interp = FileIdentity.capture(node)
        chain = LaunchChain(
            logical_command="codex",
            mode="shebang",
            launcher=launcher,
            argv_prefix=(str(node.resolve()), str(script.resolve())),
            components=(interp, launcher),
            component_argv_indexes=(0, 1),
        )
        with pytest.raises(ExecutionContractError, match="interpreter is unavailable"):
            validate_launch_chain(chain)


def test_validate_rejects_absolute_shebang_interpreter_path_mismatch() -> None:
    # Lines 329-330: interpreter resolves but to a different path than component.
    with tempfile.TemporaryDirectory() as raw:
        node = Path(raw) / "node"
        write_executable(node, b"node")
        other = Path(raw) / "runtime" / "shim"
        write_executable(other, b"shim")
        script = Path(raw) / "bin" / "codex"
        script.parent.mkdir(parents=True, exist_ok=True)
        write_executable(script, b"#!" + str(other).encode() + b"\n")
        launcher = FileIdentity.capture(script)
        interp = FileIdentity.capture(node)  # != shebang target `other`
        chain = LaunchChain(
            logical_command="codex",
            mode="shebang",
            launcher=launcher,
            argv_prefix=(str(node.resolve()), str(script.resolve())),
            components=(interp, launcher),
            component_argv_indexes=(0, 1),
        )
        _expect_incoherent(chain, match="shebang interpreter")


def test_validate_rejects_shebang_arguments_mismatch() -> None:
    # Line 337: argv_prefix != expected (interpreter, *args, script).
    with tempfile.TemporaryDirectory() as raw:
        node = Path(raw) / "node"
        write_executable(node, b"node")
        script = Path(raw) / "bin" / "codex"
        script.parent.mkdir(parents=True, exist_ok=True)
        write_executable(script, b"#!/usr/bin/env node\n")
        launcher = FileIdentity.capture(script)
        interp = FileIdentity.capture(node)
        chain = LaunchChain(
            logical_command="codex",
            mode="shebang",
            launcher=launcher,
            # expected would be (node, script); supply an extra arg -> mismatch.
            argv_prefix=(str(node.resolve()), "-X", str(script.resolve())),
            components=(interp, launcher),
            component_argv_indexes=(0, 2),
        )
        _expect_incoherent(chain, match="shebang arguments")


# ---------------------------------------------------------------------------
# _vendor_candidates branches
# ---------------------------------------------------------------------------


def _make_vendor(root: Path, pattern: str, with_vendor: bool = True) -> Path:
    vendor = (
        root / "node_modules" / "@openai" / "codex"
        / "node_modules" / "@openai" / pattern / "vendor"
        / "arch" / "bin"
    )
    if with_vendor:
        write_executable(vendor / "codex", b"vendor-binary")
    else:
        (root / "node_modules" / "@openai" / "codex"
         / "node_modules" / "@openai" / pattern).mkdir(parents=True)
    return vendor


def test_vendor_candidates_normalizes_aarch64_to_arm64_darwin() -> None:
    # Line 351.
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_vendor(root, "codex-darwin-arm64")
        launcher = root / "codex"
        write_executable(launcher, b"native")
        found = _vendor_candidates(launcher, launcher.resolve(), "darwin", "aarch64")
        assert found and found[0].name == "codex"


def test_vendor_candidates_normalizes_x86_aliases() -> None:
    for arch in ("AMD64", "x86_64"):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _make_vendor(root, "codex-linux-x64")
            launcher = root / "codex"
            write_executable(launcher, b"native")
            found = _vendor_candidates(launcher, launcher.resolve(), "linux", arch)
            assert found, f"expected x64 match for arch={arch}"


def test_vendor_candidates_unknown_platform_returns_empty() -> None:
    # Line 362.
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        launcher = root / "codex"
        write_executable(launcher, b"native")
        assert _vendor_candidates(launcher, launcher.resolve(), "solaris", "x64") == []


def test_vendor_candidates_skips_package_without_vendor_dir() -> None:
    # Line 379: package dir present but no `vendor` subdir -> skipped.
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_vendor(root, "codex-darwin-arm64", with_vendor=False)
        launcher = root / "codex"
        write_executable(launcher, b"native")
        assert _vendor_candidates(launcher, launcher.resolve(), "darwin", "arm64") == []


def test_vendor_candidates_ignores_directory_named_like_executable() -> None:
    # Line 381 branch: a glob hit that is a directory (not a regular file) is
    # skipped, while a real executable in the same tree is still resolved.
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        pkg_vendor = (
            root / "node_modules" / "@openai" / "codex"
            / "node_modules" / "@openai" / "codex-darwin-arm64" / "vendor"
        )
        # A directory whose name matches the executable.
        (pkg_vendor / "scaffold" / "codex").mkdir(parents=True)
        write_executable(pkg_vendor / "real" / "bin" / "codex", b"vendor-binary")
        launcher = root / "codex"
        write_executable(launcher, b"native")
        found = _vendor_candidates(launcher, launcher.resolve(), "darwin", "arm64")
        assert len(found) == 1
        assert found[0].read_bytes() == b"vendor-binary"


# ---------------------------------------------------------------------------
# _read_shebang_tokens branches
# ---------------------------------------------------------------------------


def test_read_shebang_tokens_cold_cache_computes_and_stores() -> None:
    # Lines 417-419: no cached digest -> sha256_and_first_line_fd + store.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "script"
        write_executable(p, b"#!/usr/bin/env node\nbody\n")
        _clear_hash_cache()
        tokens = _read_shebang_tokens(p)
        assert tokens == ("/usr/bin/env", "node")


def test_read_shebang_tokens_uses_warm_cache() -> None:
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "script"
        write_executable(p, b"#!/usr/bin/env node\n")
        identity = FileIdentity.capture(p)  # warms the cache
        tokens = _read_shebang_tokens(p, expected=identity)
        assert tokens == ("/usr/bin/env", "node")


def test_read_shebang_tokens_detects_change_during_read() -> None:
    # Line 424: stat_before != stat_after -> launcher changed mid-read.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "script"
        write_executable(p, b"#!/usr/bin/env node\n")
        real_fstat = launch.os.fstat
        calls = {"n": 0}

        def _drifting(fd: int):
            calls["n"] += 1
            observed = real_fstat(fd)
            if calls["n"] >= 2:
                return SimpleNamespace(
                    st_mode=observed.st_mode,
                    st_size=observed.st_size + 1,
                    st_mtime_ns=observed.st_mtime_ns,
                    st_ctime_ns=observed.st_ctime_ns,
                    st_dev=observed.st_dev,
                    st_ino=observed.st_ino,
                    st_atime_ns=observed.st_atime_ns,
                )
            return observed

        launch.os.fstat = _drifting
        try:
            with pytest.raises(ExecutionContractError, match="changed during shebang read"):
                _read_shebang_tokens(p)
        finally:
            launch.os.fstat = real_fstat


def test_read_shebang_tokens_detects_identity_mismatch() -> None:
    # Line 435: expected identity does not match the on-disk file.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "script"
        write_executable(p, b"#!/usr/bin/env node\n")
        wrong = FileIdentity(
            requested_path=str(p),
            resolved_path=str(p),
            sha256="0" * 64,
            size=1,
            mtime_ns=1,
            ctime_ns=1,
            device=1,
            inode=1,
        )
        with pytest.raises(ExecutionContractError, match="identity mismatch"):
            _read_shebang_tokens(p, expected=wrong)


def test_read_shebang_tokens_reports_unreadable_path() -> None:
    # Lines 440-441: os.open raises OSError -> unreadable.
    with tempfile.TemporaryDirectory() as raw:
        with pytest.raises(ExecutionContractError, match="unreadable"):
            _read_shebang_tokens(Path(raw) / "missing")


def test_read_shebang_tokens_rejects_invalid_shebang() -> None:
    # Lines 446-447: unbalanced quote -> shlex ValueError.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "script"
        write_executable(p, b"#!'unterminated\n")
        with pytest.raises(ExecutionContractError, match="invalid"):
            _read_shebang_tokens(p)


def test_read_shebang_tokens_rejects_empty_shebang() -> None:
    # Line 449: shebang with no tokens.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "script"
        write_executable(p, b"#!\n")
        with pytest.raises(ExecutionContractError, match="empty"):
            _read_shebang_tokens(p)


def test_read_shebang_tokens_no_shebang_returns_empty() -> None:
    # Line 442-443: file does not start with #! -> empty tuple.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "script"
        write_executable(p, b"plain content\n")
        assert _read_shebang_tokens(p) == ()


# ---------------------------------------------------------------------------
# _shebang_prefix branches
# ---------------------------------------------------------------------------


def test_shebang_prefix_env_ambiguous_raises() -> None:
    # Line 466: /usr/bin/env with flag-like or multi-word target.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "script"
        write_executable(p, b"#!/usr/bin/env -x\n")
        with pytest.raises(ExecutionContractError, match="ambiguous"):
            _shebang_prefix(p, search_path=None, expected=FileIdentity.capture(p))


def test_shebang_prefix_env_interpreter_unavailable_raises() -> None:
    # Line 472: env target not on the search path.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "script"
        write_executable(p, b"#!/usr/bin/env no-such-runtime-x\n")
        with pytest.raises(ExecutionContractError, match="unavailable"):
            _shebang_prefix(p, search_path="/nonexistent-empty-path", expected=FileIdentity.capture(p))


def test_shebang_prefix_non_env_missing_interpreter_raises() -> None:
    # Line 476: absolute-looking interpreter that does not exist.
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "script"
        write_executable(p, b"#!/no/such/interpreter\n")
        with pytest.raises(ExecutionContractError, match="unavailable"):
            _shebang_prefix(p, search_path=None, expected=FileIdentity.capture(p))


def test_shebang_prefix_non_env_resolves_absolute_interpreter() -> None:
    # Lines 477-481: happy path for an absolute interpreter.
    with tempfile.TemporaryDirectory() as raw:
        sh = Path("/bin/sh")
        if not sh.is_file():
            pytest.skip("/bin/sh unavailable")
        p = Path(raw) / "script"
        write_executable(p, b"#!/bin/sh -e\n")
        prefix = _shebang_prefix(p, search_path=None, expected=FileIdentity.capture(p))
        assert prefix is not None
        assert prefix[0] == str(sh.resolve())
        # The script is returned as the literal target path (un-resolved).
        assert prefix[-1] == str(p)
        assert "-e" in prefix


# ---------------------------------------------------------------------------
# resolve_codex_launch_chain entry guards
# ---------------------------------------------------------------------------


def test_resolve_rejects_relative_launcher() -> None:
    # Line 493.
    with pytest.raises(ExecutionContractError, match="must be absolute"):
        resolve_codex_launch_chain("relative/codex")


def test_resolve_win_cmd_without_vendor_fails_closed() -> None:
    # Line 513: win32 .cmd launcher with no vendor package -> unavailable.
    with tempfile.TemporaryDirectory() as raw:
        launcher = Path(raw) / "codex.cmd"
        write_executable(launcher, b"@echo off\r\n")
        with pytest.raises(ExecutionContractError, match="unavailable"):
            resolve_codex_launch_chain(str(launcher), platform="win32")
