#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
TEST_HOME = tempfile.mkdtemp(prefix="node-cross-os-path-")
os.environ["BETTER_AGENT_HOME"] = TEST_HOME
sys.path.insert(0, str(BACKEND))

from node_path_authority import (  # noqa: E402
    NodePathError,
    path_is_within_roots,
    split_cwd_roots,
    validate_cwd_roots,
)
from topology import _parse_node  # noqa: E402


WINDOWS_ROOT = "C:\\Users\\Lenovo\\code"
UNC_ROOT = "\\\\server\\share\\code"


def test_windows_roots_survive_transport() -> None:
    import node_registry_store

    roots = validate_cwd_roots([WINDOWS_ROOT, UNC_ROOT])
    assert roots == (WINDOWS_ROOT, UNC_ROOT)
    assert split_cwd_roots(
        f"{WINDOWS_ROOT};{UNC_ROOT}",
        separator=";",
    ) == roots
    spec = _parse_node(
        "windows",
        "worker_node",
        {"address": "windows", "cwd_roots": list(roots)},
    )
    assert spec.cwd_roots == roots
    record = node_registry_store.add(
        node_id="windows",
        address="windows",
        cwd_roots=list(roots),
        secret_hash="test-hash",
    )
    assert tuple(record["cwd_roots"]) == roots
    assert node_registry_store.to_spec("windows").cwd_roots == roots


def test_flavor_aware_containment() -> None:
    cases = (
        ("C:\\Users\\Lenovo\\code", (WINDOWS_ROOT,), True),
        ("c:/users/lenovo/code/project", (WINDOWS_ROOT,), True),
        ("C:\\Users\\Lenovo\\codebase", (WINDOWS_ROOT,), False),
        ("D:\\Users\\Lenovo\\code", (WINDOWS_ROOT,), False),
        ("\\\\server\\share\\code\\project", (UNC_ROOT,), True),
        ("\\\\server\\other\\code", (UNC_ROOT,), False),
        ("/srv/code/project", ("/srv/code",), True),
        ("/srv/codebase", ("/srv/code",), False),
        ("/srv/code", (WINDOWS_ROOT,), False),
    )
    for value, roots, expected in cases:
        assert path_is_within_roots(value, roots) is expected, value


def test_invalid_paths_fail_closed() -> None:
    invalid = (
        "",
        "relative/path",
        "C:drive-relative",
        "/srv/code/../secret",
        "C:\\Users\\Lenovo\\code\\..\\secret",
        "/srv/code\\mixed",
        "/srv/code\n/injected",
    )
    for value in invalid:
        try:
            validate_cwd_roots([value])
        except NodePathError:
            continue
        raise AssertionError(f"invalid node path was accepted: {value!r}")


def test_session_admission_uses_target_node_path_flavor() -> None:
    import main
    import session_detail_api
    import node_registry_store
    from topology import NodeSpec

    original_to_spec = node_registry_store.to_spec
    node_registry_store.to_spec = lambda node_id: NodeSpec(
        id=node_id,
        role="worker_node",
        address="windows",
        cwd_roots=(WINDOWS_ROOT,),
    )
    try:
        assert session_detail_api._resolve_session_node_id({
            "node_id": "windows",
            "cwd": "C:/Users/Lenovo/code/project",
        }) == "windows"
        rejected = (
            "C:\\Users\\Lenovo\\codebase",
            "C:\\Users\\Lenovo\\code\\..\\secret",
            "/Users/primary/code",
        )
        for cwd in rejected:
            try:
                session_detail_api._resolve_session_node_id({
                    "node_id": "windows",
                    "cwd": cwd,
                })
            except main.HTTPException as exc:
                assert exc.status_code == 400
                continue
            raise AssertionError(f"unsafe remote cwd was admitted: {cwd!r}")
    finally:
        node_registry_store.to_spec = original_to_spec


if __name__ == "__main__":
    try:
        test_windows_roots_survive_transport()
        test_flavor_aware_containment()
        test_invalid_paths_fail_closed()
        test_session_admission_uses_target_node_path_flavor()
        print("PASS cross-OS node path authority")
    finally:
        shutil.rmtree(TEST_HOME)
