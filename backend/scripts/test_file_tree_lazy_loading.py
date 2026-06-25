import os
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from file_browser import get_file_tree


def _count_nodes(node: dict) -> int:
    return 1 + sum(_count_nodes(child) for child in node.get("children") or [])


def test_shallow_tree_marks_unloaded_directories() -> None:
    root = Path(tempfile.mkdtemp(prefix="ba-file-tree-lazy-"))
    try:
        (root / "src" / "deep").mkdir(parents=True)
        (root / "src" / "deep" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (root / "README.md").write_text("# hi\n", encoding="utf-8")

        tree = get_file_tree(str(root), max_depth=1)
        children = {child["name"]: child for child in tree["children"]}
        assert children["src"]["type"] == "directory"
        assert children["src"]["children"] == []
        assert children["src"]["children_loaded"] is False
        assert children["src"]["has_more_children"] is True
        assert children["README.md"]["type"] == "file"

        deep = get_file_tree(str(root), max_depth=3)
        assert "app.py" in str(deep)
    finally:
        shutil.rmtree(root)


def test_shallow_tree_avoids_grandchild_payload() -> None:
    root = Path(tempfile.mkdtemp(prefix="ba-file-tree-broad-"))
    try:
        for d in range(10):
            directory = root / f"dir-{d}"
            directory.mkdir()
            for f in range(100):
                (directory / f"file-{f}.txt").write_text("x", encoding="utf-8")

        shallow = get_file_tree(str(root), max_depth=1)
        deep = get_file_tree(str(root), max_depth=2)
        assert _count_nodes(shallow) == 11
        assert _count_nodes(deep) == 1011
        assert all(child["children_loaded"] is False for child in shallow["children"])
    finally:
        shutil.rmtree(root)


def test_rpc_rejects_out_of_range_depth() -> None:
    import node_rpc_handlers

    root = Path(tempfile.mkdtemp(prefix="ba-file-tree-rpc-"))
    try:
        for max_depth in (-1, 6):
            try:
                node_rpc_handlers._rpc_get_file_tree({
                    "root": str(root),
                    "max_depth": max_depth,
                })
            except ValueError as exc:
                assert "max_depth" in str(exc)
            else:
                raise AssertionError(f"max_depth={max_depth} should be rejected")
    finally:
        shutil.rmtree(root)


def test_rpc_accepts_zero_depth() -> None:
    import node_rpc_handlers

    root = Path(tempfile.mkdtemp(prefix="ba-file-tree-rpc-zero-"))
    try:
        (root / "child.txt").write_text("x", encoding="utf-8")
        tree = node_rpc_handlers._rpc_get_file_tree({
            "root": str(root),
            "max_depth": 0,
        })
        assert tree["children"] == []
        assert tree["children_loaded"] is False
        assert tree["has_more_children"] is True
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    test_shallow_tree_marks_unloaded_directories()
    print("PASS: shallow tree marks unloaded directories")
    test_shallow_tree_avoids_grandchild_payload()
    print("PASS: shallow tree avoids grandchild payload")
    test_rpc_rejects_out_of_range_depth()
    print("PASS: rpc rejects out-of-range depth")
    test_rpc_accepts_zero_depth()
    print("PASS: rpc accepts zero depth")
