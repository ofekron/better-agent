"""Unit coverage for provider_runtime_resolver.py.

The resolver materializes a run-local capability tree (agent files + skill
trees) from an installed family-runtime-capabilities payload, then re-verifies
that tree on every subsequent resolve. It is a security chokepoint: each file
is written O_EXCL|O_NOFOLLOW under a private root and every read re-checks
private ownership, symlink-redirect, root-confinement, recorded mode, and
content.

Tests build a REAL valid payload through the production
snapshot -> stage -> install chain (no mocks of the system under test), drive
the resolver, then mutate the resolved tree on disk to reach each
security-rejection branch. Home is engaged to an isolated tempdir at import
time so no real Better Agent state is ever touched.
"""
from __future__ import annotations

import atexit
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import paths  # noqa: E402

_TEST_HOME = tempfile.mkdtemp(prefix="provider_runtime_resolver_test_")
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)
paths.engage_test_home(_TEST_HOME)

from codex_execution_common import ExecutionContractError  # noqa: E402
from paths import ba_home, make_private_directory  # noqa: E402
from provider_runtime_capability_model import (  # noqa: E402
    CAPABILITY_FILES_DIR,
    CAPABILITY_PAYLOAD_NAME,
    RunLocalCapabilities,
)
from provider_runtime_capability_snapshot import (  # noqa: E402
    snapshot_family_runtime_capabilities,
)
from provider_runtime_payload_store import (  # noqa: E402
    install_staged_family_runtime_capabilities,
    stage_family_runtime_capabilities,
)
import provider_runtime_resolver as resolver  # noqa: E402

_PLAN = {"harness": {}, "tools": ["probe_tool"], "mcp_servers": []}
_counter = {"n": 0}


def _rid(label: str) -> str:
    _counter["n"] += 1
    return f"{label}-{_counter['n']}"


def _skill_source(name: str, *, nested: bool = False) -> Path:
    root = Path(_TEST_HOME) / "sources" / name
    root.mkdir(parents=True, exist_ok=True)
    skill_md = root / "SKILL.md"
    skill_md.write_text(f"# {name}\n")
    skill_md.chmod(0o400)
    if nested:
        sub = root / "lib"
        sub.mkdir(exist_ok=True)
        helper = sub / "helper.md"
        helper.write_text("helper\n")
        helper.chmod(0o400)
    return root


def _agent_source(name: str) -> Path:
    path = Path(_TEST_HOME) / "sources" / f"{name}.agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# agent {name}\n")
    path.chmod(0o400)
    return path


def _fresh_run_dir(run_id: str) -> Path:
    runs = ba_home() / "runs"
    runs.mkdir(mode=0o700, exist_ok=True)
    make_private_directory(runs)
    run_dir = runs / run_id
    run_dir.mkdir(mode=0o700, exist_ok=True)
    make_private_directory(run_dir)
    return run_dir


def _install(
    run_id: str,
    *,
    skill_sources: dict[str, Path] | None = None,
    agent_sources: dict[str, Path] | None = None,
    family: str = "claude",
) -> tuple[Path, dict]:
    prepared = snapshot_family_runtime_capabilities(
        family=family,
        skill_sources=skill_sources or {},
        agent_sources=agent_sources or {},
        resolved_plan=_PLAN,
        extension_state={},
        installation_decisions={},
    )
    stage_family_runtime_capabilities(run_id, prepared)
    run_dir = _fresh_run_dir(run_id)
    install_staged_family_runtime_capabilities(
        run_dir,
        run_id=run_id,
        manifest=prepared.manifest,
    )
    return run_dir, prepared.manifest


def _install_resolved(
    label: str,
    *,
    skill_sources: dict[str, Path],
    agent_sources: dict[str, Path] | None = None,
    family: str = "claude",
) -> tuple[Path, dict]:
    """Install a payload AND materialize its resolved tree on disk.

    ``install`` only drops the payload file; the ``runtime-capabilities/`` tree
    is built lazily by the first ``resolve_run_local_capabilities`` call, so
    corruption/cleanup tests must drive that resolve before mutating the tree.
    """
    run_dir, manifest = _install(
        _rid(label),
        skill_sources=skill_sources,
        agent_sources=agent_sources,
        family=family,
    )
    resolver.resolve_run_local_capabilities(run_dir, manifest)
    return run_dir, manifest


def test_resolve_builds_tree_for_skills_and_agents() -> None:
    run_dir, manifest = _install(
        _rid("build"),
        skill_sources={"alpha": _skill_source("alpha", nested=True)},
        agent_sources={"scout": _agent_source("scout")},
    )
    result = resolver.resolve_run_local_capabilities(run_dir, manifest)

    assert isinstance(result, RunLocalCapabilities)
    assert result.tool_names == ("probe_tool",)
    assert result.prewarm_status == {}

    target = run_dir / CAPABILITY_FILES_DIR
    assert set(result.skill_dirs) == {"alpha"}
    assert result.skill_dirs["alpha"] == target / "skills" / "alpha"
    assert set(result.agent_files) == {"scout"}
    assert result.agent_files["scout"] == target / "agents" / "scout"

    skill_md = result.skill_dirs["alpha"] / "SKILL.md"
    helper = result.skill_dirs["alpha"] / "lib" / "helper.md"
    agent_file = result.agent_files["scout"]
    assert skill_md.read_text() == "# alpha\n"
    assert helper.read_text() == "helper\n"
    assert agent_file.read_text() == "# agent scout\n"
    for path in (skill_md, helper, agent_file):
        assert stat.S_IMODE(path.stat().st_mode) == 0o400


def test_resolve_skill_only_agy_family() -> None:
    run_dir, manifest = _install(
        _rid("agy"),
        skill_sources={"bravo": _skill_source("bravo")},
        family="agy",
    )
    result = resolver.resolve_run_local_capabilities(run_dir, manifest)
    assert set(result.skill_dirs) == {"bravo"}
    assert result.agent_files == {}


def test_resolve_is_idempotent_when_tree_exists() -> None:
    run_dir, manifest = _install(
        _rid("idem"),
        skill_sources={"alpha": _skill_source("alpha")},
    )
    first = resolver.resolve_run_local_capabilities(run_dir, manifest)
    second = resolver.resolve_run_local_capabilities(run_dir, manifest)
    assert second.skill_dirs == first.skill_dirs
    assert second.agent_files == first.agent_files


def test_resolve_rejects_non_private_tree() -> None:
    run_dir, manifest = _install_resolved(
        "nonpriv-tree",
        skill_sources={"alpha": _skill_source("alpha")},
    )
    (run_dir / CAPABILITY_FILES_DIR).chmod(0o755)
    with pytest.raises(ExecutionContractError):
        resolver.resolve_run_local_capabilities(run_dir, manifest)


def test_resolve_rejects_symlinked_tree() -> None:
    run_dir, manifest = _install_resolved(
        "symlink-tree",
        skill_sources={"alpha": _skill_source("alpha")},
    )
    target = run_dir / CAPABILITY_FILES_DIR
    decoy = run_dir / "decoy-tree"
    decoy.mkdir(mode=0o700)
    make_private_directory(decoy)
    shutil.rmtree(target)
    target.symlink_to(decoy)
    with pytest.raises(ExecutionContractError):
        resolver.resolve_run_local_capabilities(run_dir, manifest)


def test_resolve_rejects_non_dir_tree() -> None:
    run_dir, manifest = _install_resolved(
        "nondir-tree",
        skill_sources={"alpha": _skill_source("alpha")},
    )
    target = run_dir / CAPABILITY_FILES_DIR
    shutil.rmtree(target)
    target.write_bytes(b"not a directory")
    target.chmod(0o600)
    with pytest.raises(ExecutionContractError):
        resolver.resolve_run_local_capabilities(run_dir, manifest)


def test_resolve_rejects_non_private_capability_file() -> None:
    run_dir, manifest = _install_resolved(
        "nonpriv-file",
        skill_sources={"alpha": _skill_source("alpha")},
    )
    skill_md = run_dir / CAPABILITY_FILES_DIR / "skills" / "alpha" / "SKILL.md"
    skill_md.chmod(0o644)
    with pytest.raises(ExecutionContractError):
        resolver.resolve_run_local_capabilities(run_dir, manifest)


def test_resolve_rejects_capability_file_mode_mismatch() -> None:
    run_dir, manifest = _install_resolved(
        "mode-mismatch",
        skill_sources={"alpha": _skill_source("alpha")},
    )
    skill_md = run_dir / CAPABILITY_FILES_DIR / "skills" / "alpha" / "SKILL.md"
    skill_md.chmod(0o500)
    with pytest.raises(ExecutionContractError):
        resolver.resolve_run_local_capabilities(run_dir, manifest)


def test_resolve_rejects_capability_file_content_mismatch() -> None:
    run_dir, manifest = _install_resolved(
        "content-mismatch",
        skill_sources={"alpha": _skill_source("alpha")},
    )
    skill_md = run_dir / CAPABILITY_FILES_DIR / "skills" / "alpha" / "SKILL.md"
    skill_md.chmod(0o600)
    skill_md.write_bytes(b"tampered\n")
    skill_md.chmod(0o400)
    with pytest.raises(ExecutionContractError):
        resolver.resolve_run_local_capabilities(run_dir, manifest)


def test_resolve_rejects_unsafe_capability_directory() -> None:
    run_dir, manifest = _install_resolved(
        "unsafe-dir",
        skill_sources={"alpha": _skill_source("alpha")},
    )
    alpha_dir = run_dir / CAPABILITY_FILES_DIR / "skills" / "alpha"
    alpha_dir.chmod(0o755)
    with pytest.raises(ExecutionContractError):
        resolver.resolve_run_local_capabilities(run_dir, manifest)


def test_cleanup_removes_tree_and_payload() -> None:
    run_dir, _ = _install_resolved(
        "clean",
        skill_sources={"alpha": _skill_source("alpha")},
    )
    target = run_dir / CAPABILITY_FILES_DIR
    payload = run_dir / CAPABILITY_PAYLOAD_NAME
    assert target.is_dir()
    assert payload.is_file()
    resolver.cleanup_installed_family_runtime_capabilities(run_dir)
    assert not target.exists()
    assert not payload.exists()


def test_cleanup_when_files_dir_absent_unlinks_payload() -> None:
    run_dir, _ = _install_resolved(
        "clean-no-files",
        skill_sources={"alpha": _skill_source("alpha")},
    )
    target = run_dir / CAPABILITY_FILES_DIR
    payload = run_dir / CAPABILITY_PAYLOAD_NAME
    shutil.rmtree(target)
    resolver.cleanup_installed_family_runtime_capabilities(run_dir)
    assert not target.exists()
    assert not payload.exists()


def test_cleanup_payload_missing_is_idempotent() -> None:
    run_dir, _ = _install(
        _rid("clean-missing-payload"),
        skill_sources={"alpha": _skill_source("alpha")},
    )
    payload = run_dir / CAPABILITY_PAYLOAD_NAME
    payload.unlink()
    resolver.cleanup_installed_family_runtime_capabilities(run_dir)
    assert not payload.exists()
    assert not (run_dir / CAPABILITY_FILES_DIR).exists()


def test_cleanup_rejects_symlinked_tree() -> None:
    run_dir, _ = _install_resolved(
        "clean-symlink",
        skill_sources={"alpha": _skill_source("alpha")},
    )
    target = run_dir / CAPABILITY_FILES_DIR
    decoy = run_dir / "decoy"
    decoy.mkdir(mode=0o700)
    make_private_directory(decoy)
    shutil.rmtree(target)
    target.symlink_to(decoy)
    with pytest.raises(ExecutionContractError):
        resolver.cleanup_installed_family_runtime_capabilities(run_dir)
