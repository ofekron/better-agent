"""Regression lock: every text-mode file I/O in production backend
modules MUST declare encoding="utf-8".

Without an explicit encoding, CPython falls back to
locale.getpreferredencoding() — UTF-8 on macOS/Linux but cp1252 on a
default Windows install. That divergence silently corrupts non-ASCII
content (a writer emits utf-8 bytes a reader can't decode) and can
raise UnicodeDecode/EncodeError on Windows — across session state,
jsonl ingestion, recovery, traces, config and tokens.

Covered call shapes (all OS-independent, parsed from source):
  - open()/os.fdopen()      builtin-style; mode at arg[1], text → encoding required
  - path.open()             pathlib; mode at arg[0], text → encoding required
  - .read_text()/.write_text()                       always text → encoding required
    (encoding may be passed by keyword OR positionally — arg[0] for
    read_text, arg[1] for write_text, whose arg[0] is the data payload)
Exempt (binary or non-file — encoding meaningless):
  - any mode containing 'b'; .read_bytes()/.write_bytes()
  - .open() on a receiver that is NOT plainly a pathlib Path:
    tarfile/gzip/zipfile/bz2/lzma/io modules (binary archives),
    os (os.open is a syscall returning an fd), urllib opener objects
    (build_opener(...).open(...) — network), custom objects
    (e.g. self._wal.open()), and any other non-Path expression.
  - a non-literal mode that already declares encoding= (safe either way).

Scope: all backend/*.py and subpackages EXCEPT backend/scripts/ (tests
read their own fixtures and set BETTER_AGENT_HOME tempdirs). Parses
source (no mocks, OS-independent): fails before the fix, passes after,
and catches any future encoding-less text I/O.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
_BUILTIN_OPENERS = {"open", "fdopen"}
_TEXT_METHODS = {"read_text", "write_text"}
_BINARY_METHODS = {"read_bytes", "write_bytes"}
# Modules whose `.open()`/`.open` is binary archive/stream I/O, not pathlib.
_BINARY_OPEN_MODULES = {"tarfile", "gzip", "zipfile", "bz2", "lzma", "io"}
_EXCLUDE = {"scripts", ".venv", "venv", "site-packages", "node_modules", "__pycache__"}


def _has_encoding_kw(call: ast.Call) -> bool:
    return any(kw.arg == "encoding" for kw in call.keywords)


def _declares_encoding(call: ast.Call, name: str) -> bool:
    """For text methods, encoding may be keyword OR positional."""
    if _has_encoding_kw(call):
        return True
    if name == "read_text":
        return len(call.args) >= 1          # arg[0] = encoding
    if name == "write_text":
        return len(call.args) >= 2          # arg[0] = data, arg[1] = encoding
    return False


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


def _is_pathlib_open_receiver(value: ast.AST) -> bool:
    """True only when `value.open(...)` is plausibly a pathlib Path.

    `.open()` is overloaded — tarfile/gzip modules, urllib openers, file
    and socket objects, and custom objects all expose it — so we flag only
    receivers that are plainly Path-typed: a bare Name, a `/`-joined BinOp
    (e.g. `dir / "f"`), or a `Path(...)` construction. Anything else is
    ambiguous and skipped rather than false-flagged.
    """
    if isinstance(value, ast.Name):
        return value.id not in _BINARY_OPEN_MODULES and value.id != "os"
    if isinstance(value, ast.BinOp):
        return True
    if isinstance(value, ast.Call):
        f = value.func
        if isinstance(f, ast.Name) and f.id == "Path":
            return True
        if isinstance(f, ast.Attribute) and f.attr == "Path":
            return True
        return False
    return False


def _check_call(rel: str, node: ast.Call) -> str | None:
    func = node.func
    # pathlib `.open(...)` (attribute) — only when receiver is plainly a Path.
    if isinstance(func, ast.Attribute) and func.attr == "open":
        if not _is_pathlib_open_receiver(func.value):
            return None
        mode = _literal_mode(node, mode_index=0)
        if mode is None:
            return None if _has_encoding_kw(node) else f"{rel}:{node.lineno} .open() non-literal mode (prove text/binary)"
        if "b" in mode:
            return None
        return None if _has_encoding_kw(node) else f"{rel}:{node.lineno} text-mode .open() missing encoding="
    name = func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None
    )
    if name is None or name in _BINARY_METHODS:
        return None
    if name in _TEXT_METHODS:
        return None if _declares_encoding(node, name) else f"{rel}:{node.lineno} {name}() missing encoding="
    if name in _BUILTIN_OPENERS:
        mode = _literal_mode(node, mode_index=1)
        if mode is None:
            return None if _has_encoding_kw(node) else f"{rel}:{node.lineno} {name}() non-literal mode (prove text/binary)"
        if "b" in mode:
            return None
        return None if _has_encoding_kw(node) else f"{rel}:{node.lineno} text-mode {name}() missing encoding="
    return None


def _scan_violations() -> list[str]:
    files = [
        p for p in BACKEND.rglob("*.py")
        if not (_EXCLUDE & set(p.relative_to(BACKEND).parts))
    ]
    violations: list[str] = []
    for path in sorted(files):
        rel = str(path.relative_to(BACKEND))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                v = _check_call(rel, node)
                if v:
                    violations.append(v)
    return violations


def test_all_text_io_declares_utf8_encoding() -> None:
    violations = _scan_violations()
    assert not violations, (
        f"{len(violations)} encoding-less text I/O site(s) in production "
        f"backend modules:\n  " + "\n  ".join(violations)
    )
