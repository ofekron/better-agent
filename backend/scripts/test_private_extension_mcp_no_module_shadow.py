"""Private extensions with a local `mcp/` package must launch via `python` file path, not `module`.

Background: declaring `module: "mcp.server"` makes the launcher run `python -m mcp.server`,
which resolves to the SDK package on PYTHONPATH (regular package) and shadows the
extension's namespace-package `mcp/`. The SDK's generic stdio server starts with zero
tools registered — the MCP handshake succeeds but `list_tools` returns empty.

Run with:
    cd backend && .venv/bin/python scripts/test_private_extension_mcp_no_module_shadow.py
"""

from __future__ import annotations

import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PRIVATE_EXTENSIONS = REPO / "better-agent-private" / "extensions"

AFFECTED = (
    "requirements",
    "canvas",
    "browser-harness",
    "credential-broker",
    "project-structure",
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(ext: str) -> tuple[bool, str]:
    manifest_path = PRIVATE_EXTENSIONS / ext / "better-agent-extension.json"
    if not manifest_path.exists():
        return False, f"manifest missing at {manifest_path}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mcp_entries = manifest.get("entrypoints", {}).get("mcp", []) or []
    if not mcp_entries:
        return False, "manifest has no mcp entrypoints"
    entry = mcp_entries[0]
    has_module = bool(entry.get("module"))
    python_value = entry.get("python") or ""
    if has_module:
        return False, f"entry uses 'module={entry.get('module')!r}' which shadows SDK"
    if python_value != "mcp/server.py":
        return False, f"entry 'python' is {python_value!r}, expected 'mcp/server.py'"
    server_file = PRIVATE_EXTENSIONS / ext / "mcp" / "server.py"
    if not server_file.exists():
        return False, f"server file missing at {server_file}"
    return True, ""


def _run() -> bool:
    results = [(ext, *_check(ext)) for ext in AFFECTED]
    for ext, ok, msg in results:
        tag = PASS if ok else FAIL
        suffix = "" if ok else f" - {msg}"
        print(f"  {tag} {ext}{suffix}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if _run() else 1)
