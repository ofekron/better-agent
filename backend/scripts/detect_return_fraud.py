"""Detect return-fraud in pytest-collectable test modules.

A test function is FRAUDULENT when it returns a non-None value: pytest treats
any non-None return as a passed test (emitting only PytestReturnNotNoneWarning),
so the function's internal pass/fail logic — if it computes a bool and returns
it instead of asserting — is never enforced. Whole files of such tests report
"N passed" while being entirely broken.

This walks the AST and flags `def test_*` functions (module-level and inside
classes) whose OWN body contains a top-level `return <non-None>`. Returns
inside nested helper functions/classes are ignored — only a return that
escapes the test function itself is fraud.

Usage:
    python scripts/detect_return_fraud.py            # report all hits
    python scripts/detect_return_fraud.py --summary  # counts only
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent


def _return_is_non_none(node: ast.Return) -> bool:
    value = node.value
    if value is None:
        return False
    if isinstance(value, ast.Constant) and value.value is None:
        return False
    return True


def _fraud_returns(func: ast.FunctionDef) -> list[ast.Return]:
    """Top-level non-None returns directly in func's body (not nested defs)."""
    hits: list[ast.Return] = []
    for stmt in func.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, ast.Return) and _return_is_non_none(stmt):
            hits.append(stmt)
    return hits


def _param_count(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Parameters pytest would try to resolve as fixtures.

    A test with zero params is always RUN by pytest (nothing to skip on); a
    test with non-fixture params is SKIPPED by the standalone-runner heuristic
    instead of being fraudulently passed. `self` on a method is excluded.
    """
    args = func.args
    total = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
    if args.vararg:
        total += 1
    if args.kwarg:
        total += 1
    if args.args and args.args[0].arg == "self":
        total -= 1
    return total


def detect(path: Path) -> list[tuple[str, int, str, bool, bool, int]]:
    """Return [(func_name, lineno, return_src, has_assert, is_dangerous, params)]."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []

    out: list[tuple[str, int, str, str, bool, int]] = []
    src_lines = path.read_text(encoding="utf-8").splitlines()

    def inspect_func(func: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if not func.name.startswith("test_"):
            return
        hits = _fraud_returns(func)
        if not hits:
            return
        # Fraud-without-assert is the dangerous case (return replaces all
        # checking); return-after-assert is only a style warning.
        has_assert = any(isinstance(stmt, ast.Assert) for stmt in ast.walk(func))
        params = _param_count(func)
        # Pytest runs a zero-param test unconditionally; if it also lacks any
        # assert, its return value is the ONLY signal — and pytest ignores it.
        dangerous = (not has_assert) and params == 0
        for ret in hits:
            snippet = ast.get_source_segment("".join(l + "\n" for l in src_lines), ret)
            out.append((func.name, ret.lineno, (snippet or "").strip(), has_assert, dangerous, params))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inspect_func(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inspect_func(sub)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--no-assert-only", action="store_true",
                        help="only tests that lack any assert")
    parser.add_argument("--dangerous", action="store_true",
                        help="only zero-param tests lacking any assert: pytest runs "
                             "these and falsely reports them as passed")
    args = parser.parse_args()

    files = sorted(_SCRIPTS.glob("test_*.py"))
    per_file: dict[Path, list] = {}
    for path in files:
        hits = detect(path)
        if args.dangerous:
            hits = [h for h in hits if h[4]]
        elif args.no_assert_only:
            hits = [h for h in hits if not h[3]]
        if hits:
            per_file[path] = hits

    total = sum(len(h) for h in per_file.values())
    if args.summary:
        print(f"files_with_fraud={len(per_file)} fraudulent_tests={total}")
        for path, hits in sorted(per_file.items(), key=lambda kv: -len(kv[1])):
            print(f"{len(hits):4d}  {path.name}")
        return 0

    for path, hits in sorted(per_file.items()):
        for func_name, lineno, snippet, has_assert, _dangerous, params in hits:
            flag = "DANGEROUS" if (not has_assert and params == 0) else (
                "NO-ASSERT" if not has_assert else "has-assert")
            print(f"{path.name}:{lineno}\t{func_name}\t[{flag},params={params}]\t{snippet}")
    print(f"\n--- {total} fraudulent test functions across {len(per_file)} files ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
