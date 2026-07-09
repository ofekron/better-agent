from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-node-path-guard-")
os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="bc-test-node-path-guard-os-home-"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import node_rpc_handlers  # noqa: E402


class _Spec:
    def __init__(self, roots):
        self.cwd_roots = tuple(roots)


def _install_topology(handlers, root: str) -> None:
    import topology as _topology

    handlers._local_node_id = lambda: "worker-1"
    _topology.load_topology = lambda: {"worker-1": _Spec([root])}


def test_traversal_out_of_root_is_rejected() -> None:
    base = Path(tempfile.mkdtemp(prefix="bc-cwd-root-"))
    root = base / "allowed"
    root.mkdir()
    outside = base / "secret.txt"
    outside.write_text("top secret\n", encoding="utf-8")
    _install_topology(node_rpc_handlers, str(root))

    escape = f"{root}/../secret.txt"
    try:
        node_rpc_handlers._assert_within_cwd_roots(escape)
    except ValueError:
        pass
    else:
        raise AssertionError("`..` traversal escaped cwd_roots allowlist")

    # A path genuinely inside the root must still pass.
    node_rpc_handlers._assert_within_cwd_roots(str(root / "sub" / "file.txt"))
    node_rpc_handlers._assert_within_cwd_roots(str(root))


def test_symlink_escape_is_rejected() -> None:
    base = Path(tempfile.mkdtemp(prefix="bc-cwd-symlink-"))
    root = base / "allowed"
    root.mkdir()
    target = base / "outside"
    target.mkdir()
    (target / "loot.txt").write_text("loot\n", encoding="utf-8")
    link = root / "escape"
    try:
        link.symlink_to(target)
    except OSError:
        return  # platform without symlink support; skip
    _install_topology(node_rpc_handlers, str(root))

    try:
        node_rpc_handlers._assert_within_cwd_roots(str(link / "loot.txt"))
    except ValueError:
        pass
    else:
        raise AssertionError("symlink escape out of cwd_roots was allowed")


def test_empty_roots_is_wildcard() -> None:
    import topology as _topology

    node_rpc_handlers._local_node_id = lambda: "primary"
    _topology.load_topology = lambda: {"primary": _Spec([])}
    node_rpc_handlers._assert_within_cwd_roots("/anywhere/at/all.txt")


if __name__ == "__main__":
    test_traversal_out_of_root_is_rejected()
    test_symlink_escape_is_rejected()
    test_empty_roots_is_wildcard()
    print("node_rpc path-guard tests passed")
