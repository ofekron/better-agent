"""Browser Test runtime belongs to the private extension.

Run with:
    cd backend && .venv/bin/python scripts/test_browser_test_extension_boundary.py
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _run() -> bool:
    main = (BACKEND / "main.py").read_text(encoding="utf-8")
    results = [
        (
            "public browser-test route is removed",
            "/api/internal/browser-test" not in main,
            "old route remains",
        ),
        (
            "public browser-test runtime module is removed",
            not (BACKEND / "orchs" / "_browser_test.py").exists(),
            "backend/orchs/_browser_test.py still exists",
        ),
        (
            "generic managed-run substrate exists",
            "/api/internal/managed-runs/run" in main
            and "/api/internal/managed-runs/create-session" in main,
            "managed-run endpoints missing",
        ),
        (
            "managed-run sessions are extension-owned",
            "extension_session_ownership.is_owner(managed_session_id, extension_id)" in main,
            "managed-run endpoint does not verify extension session ownership",
        ),
        (
            "managed-run env is manifest allowlisted",
            "extra_env key is not declared in permissions.managed_run_env" in main,
            "managed-run endpoint does not enforce manifest env allowlist",
        ),
    ]
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' - ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if _run() else 1)
