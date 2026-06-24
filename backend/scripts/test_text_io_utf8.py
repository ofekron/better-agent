"""Regression lock: every text-mode file I/O in production backend
modules MUST declare encoding="utf-8".

Without an explicit encoding, CPython falls back to
locale.getpreferredencoding() — UTF-8 on macOS/Linux but cp1252 on a
default Windows install. That divergence silently corrupts non-ASCII
content (a writer emits utf-8 bytes a reader can't decode) and can
raise UnicodeDecode/EncodeError on Windows — across session state,
jsonl ingestion, recovery, traces, config and tokens.

Covered call shapes:
  - open()/os.fdopen()      builtin-style, mode at arg[1]; text → encoding required
  - path.open()             pathlib, mode at arg[0]; text → encoding required
  - .read_text()/.write_text()                       always text → encoding required
Exempt (binary — encoding meaningless):
  - any mode containing 'b'; .read_bytes()/.write_bytes()

Scope: all backend/*.py and subpackages EXCEPT backend/scripts/ (tests
read their own fixtures and set BETTER_CLAUDE_HOME tempdirs). Parses
source (no mocks, OS-independent): fails before the fix, passes after,
and catches any future encoding-less text I/O.
"""

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
# builtin-style: mode is the 2nd positional arg (1st is file/fd).
_BUILTIN_OPENERS = {"open", "fdopen"}
_TEXT_METHODS = {"read_text", "write_text"}
_BINARY_METHODS = {"read_bytes", "write_bytes"}


def _has_encoding(call: ast.Call) -> bool:
    return any(kw.arg == "encoding" for kw in call.keywords)


def _literal_mode(call: ast.Call, mode_index: int) -> str | None:
    """Literal mode string, "" if mode defaults (text), None if non-literal."""
    for kw in call.keywords:
        if kw.arg == "mode":
            v = kw.value
            return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else None
    if len(call.args) > mode_index:
        v = call.args[mode_index]
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            return v.value
        return None
    return ""


def _violation(fname: str, node: ast.Call) -> str | None:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None
    )
    if name is None:
        return None
    if name in _BINARY_METHODS:
        return None
    if name in _TEXT_METHODS:
        return f"{fname}:{node.lineno} {name}() missing encoding=" if not _has_encoding(node) else None
    if name not in _BUILTIN_OPENERS:
        return None
    mode = _literal_mode(node, mode_index=1)
    if mode is None:
        return f"{fname}:{node.lineno} {name}() non-literal mode (prove text/binary)"
    if "b" in mode:
        return None
    return f"{fname}:{node.lineno} text-mode {name}() missing encoding=" if not _has_encoding(node) else None


def _pathlib_open_violation(fname: str, node: ast.Call) -> str | None:
    """`x.open(...)` (pathlib) — mode at arg[0]."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "open"):
        return None
    mode = _literal_mode(node, mode_index=0)
    if mode is None:
        return f"{fname}:{node.lineno} .open() non-literal mode (prove text/binary)"
    if "b" in mode:
        return None
    return f"{fname}:{node.lineno} text-mode .open() missing encoding=" if not _has_encoding(node) else None


def main() -> int:
    _EXCLUDE = {"scripts", ".venv", "venv", "site-packages", "node_modules", "__pycache__"}
    files = [
        p for p in BACKEND.rglob("*.py")
        if not (_EXCLUDE & set(p.relative_to(BACKEND).parts))
    ]
    violations: list[str] = []
    for path in sorted(files):
        rel = str(path.relative_to(BACKEND))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # pathlib .open (attribute) — mode at arg[0]
            if isinstance(func, ast.Attribute) and func.attr == "open":
                # os.open is the low-level syscall (returns an int fd, no
                # encoding) — not a text opener.
                if isinstance(func.value, ast.Name) and func.value.id == "os":
                    continue
                v = _pathlib_open_violation(rel, node)
            else:
                v = _violation(rel, node)
            if v:
                violations.append(v)

    if violations:
        print(f"FAIL — {len(violations)} encoding-less text I/O site(s):")
        for v in violations:
            print(f"  {v}")
        return 1
    print(f"PASS — all text I/O in {len(files)} production modules declares encoding")
    return 0


if __name__ == "__main__":
    sys.exit(main())
