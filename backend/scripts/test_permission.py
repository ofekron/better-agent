"""Permission selector: per-provider-native vocabularies, resolution, and the
runner-level flag translation. Pure-logic — no CLI subprocess.

Run:  python3 backend/scripts/test_permission.py
"""
import os
import sys

# Isolate state so the test never touches the developer's real home.
os.environ["BETTER_AGENT_HOME"] = os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "ba_test_permission"
)
os.makedirs(os.environ["BETTER_AGENT_HOME"], exist_ok=True)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import permission
from runner_codex import (
    _codex_approval_policy,
    _codex_sandbox_mode,
    _codex_sandbox_policy,
)

import config_store
import session_manager

_failures: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        _failures.append(f"{name}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {name}")


# ── vocabularies ────────────────────────────────────────────────────────
check("claude modes", permission.CLAUDE_PERMISSION_MODES,
      ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"))
check("codex approval", permission.CODEX_APPROVAL_POLICIES,
      ("untrusted", "on-request", "on-failure", "never"))
check("codex sandbox", permission.CODEX_SANDBOX_MODES,
      ("read-only", "workspace-write", "danger-full-access"))
check("gemini modes", permission.GEMINI_APPROVAL_MODES,
      ("default", "auto_edit", "yolo", "plan"))

# ── defaults preserve prior bypass behavior ────────────────────────────
check("claude default", permission.default_permission_for_kind("claude"),
      {"mode": "bypassPermissions"})
check("codex default", permission.default_permission_for_kind("codex"),
      {"approval": "never", "sandbox": "danger-full-access"})
check("gemini default", permission.default_permission_for_kind("gemini"),
      {"mode": "yolo"})

# ── normalize: inherit semantics ───────────────────────────────────────
check("normalize None", permission.normalize_permission("claude", None), None)
check("normalize empty dict", permission.normalize_permission("claude", {}), None)
check("normalize valid", permission.normalize_permission("claude", {"mode": "plan"}),
      {"mode": "plan"})
# Unknown axis value coerces to the kind default for that axis (not crash).
check("normalize bogus axis value", permission.normalize_permission("codex", {"approval": "bogus", "sandbox": "read-only"}),
      {"approval": "never", "sandbox": "read-only"})
check("normalize unknown kind", permission.normalize_permission("nope", {"mode": "x"}), None)

# ── resolve: override → provider default → kind default ────────────────
check("resolve override wins", permission.resolve_permission("gemini", {"mode": "plan"}, {"mode": "yolo"}),
      {"mode": "plan"})
check("resolve falls to provider default", permission.resolve_permission("gemini", None, {"mode": "plan"}),
      {"mode": "plan"})
check("resolve falls to kind default", permission.resolve_permission("gemini", None, None),
      {"mode": "yolo"})

# ── codex runner flag translation (kebab → camelCase sandbox type) ─────
check("codex approval policy", _codex_approval_policy({"approval": "on-request"}), "on-request")
check("codex approval missing", _codex_approval_policy({}), "never")
check("codex sandbox mode", _codex_sandbox_mode({"sandbox": "read-only"}), "read-only")
check("codex policy read-only", _codex_sandbox_policy("read-only"), {"type": "readOnly"})
check("codex policy workspace-write", _codex_sandbox_policy("workspace-write"), {"type": "workspaceWrite"})
check("codex policy danger", _codex_sandbox_policy("danger-full-access"), {"type": "dangerFullAccess"})

# ── session layer: create + set_selectors ──────────────────────────────
mgr = session_manager.SessionManager()
providers = config_store.list_providers()["providers"]
claude_pid = next(p["id"] for p in providers if p["kind"] == "claude")
codex_pid = next(p["id"] for p in providers if p["kind"] == "codex")

s = mgr.create(name="t", provider_id=claude_pid, permission={"mode": "plan"})
check("session override persisted", s["permission"], {"mode": "plan"})
s_inherit = mgr.create(name="t2", provider_id=claude_pid, permission={})
check("session empty = inherit ({})", s_inherit["permission"], {})
s_codex = mgr.create(name="c", provider_id=codex_pid, permission={"approval": "on-request", "sandbox": "read-only"})
check("codex session override", s_codex["permission"], {"approval": "on-request", "sandbox": "read-only"})

upd = mgr.set_selectors(s["id"], permission={"mode": "acceptEdits"})
check("set_selectors update", upd["permission"], {"mode": "acceptEdits"})
# Switching provider at the session layer leaves permission as {} (inherit);
# the run-time resolver then yields the new provider's default. (The API
# layer in main.py additionally writes the provider's default_permission on
# a provider switch — tested implicitly by that being a plain dict copy.)
sw = mgr.set_selectors(s_inherit["id"], provider_id=codex_pid)
check("provider switch leaves inherit ({})", sw["permission"], {})
check("run resolves codex default after switch",
      permission.resolve_for_run(
          sess_rec={"provider_id": codex_pid, "permission": {}},
          worker_sess_rec=None,
          is_worker=False,
          fallback_kind="codex",
      ),
      {"approval": "never", "sandbox": "danger-full-access"})

if _failures:
    print("\nFAILED:")
    for f in _failures:
        print(" -", f)
    sys.exit(1)
print("\nALL PASS")
