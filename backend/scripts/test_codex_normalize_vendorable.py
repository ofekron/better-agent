"""codex_normalize must stay stdlib-only (BA-import-free, vendorable).

Invariant from the ingest-consolidation plan: shared provider
normalizers are consumed by the live runner, offline tailing/replay,
and the vendored transcript-search product, so they may not import
anything outside the standard library.
"""
import ast
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent / "codex_normalize.py"


def test_codex_normalize_is_stdlib_only():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    bad: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                bad.add("." * node.level + (node.module or ""))
                continue
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        bad.update(n for n in names if n and n not in sys.stdlib_module_names)
    assert not bad, f"codex_normalize imports non-stdlib modules: {sorted(bad)}"
