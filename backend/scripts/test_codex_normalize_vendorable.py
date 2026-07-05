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


def main() -> int:
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
    if bad:
        print(f"FAIL codex_normalize imports non-stdlib modules: {sorted(bad)}")
        return 1
    print("PASS codex_normalize is stdlib-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
