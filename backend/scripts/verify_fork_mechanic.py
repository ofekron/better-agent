"""Fork-mechanic verification gate for the workers redesign.

The redesign hinges on this assumption:
  1. Resume an existing claude session S0 with fork_session=True
     → claude_agent_sdk mints a NEW session_id F1 whose jsonl includes
       S0's history (the fork point) plus the new turn.
  2. After F1 exists, resume(session_id=F1, fork_session=False) appends
     turns to F1's jsonl just like any normal resume.
  3. S0's jsonl is unchanged by step 1 (forked, not mutated) and remains
     unchanged by step 2 (we resumed F1, not S0).

If any of those three properties is false, the per-(caller, worker)
fork-once-then-resume design is broken and we must redesign before
implementing anything else.

Run from /workspace/better-agent/backend with the project venv:

    .venv/bin/python scripts/verify_fork_mechanic.py

Exit 0 = all assertions pass, plan stands.
Exit 1 = at least one assertion failed, STOP and reopen design.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

# Importable from backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)

def _claude_projects() -> Path:
    """Honor CLAUDE_CONFIG_DIR for users whose claude config lives
    somewhere other than ~/.claude (e.g. ~/.claude-zai)."""
    import os as _os
    raw = _os.environ.get("CLAUDE_CONFIG_DIR", "")
    base = Path(_os.path.expandvars(raw)).expanduser() if raw else Path.home() / ".claude"
    return base / "projects"


def jsonl_path(cwd: str, sid: str) -> Path:
    """Locate the jsonl by sid via glob — claude CLI's cwd-encoding rules
    (e.g. underscore → dash) don't always match worker_store.encode_cwd's
    naive replacement. We trust the disk over the encoder."""
    base = _claude_projects()
    matches = list(base.glob(f"*/{sid}.jsonl"))
    if matches:
        return matches[0]
    return base / "_unresolved_" / f"{sid}.jsonl"


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as f:
        return sum(1 for _ in f)


def read_user_messages(path: Path) -> list[str]:
    """Return the text of every 'user' role message in this jsonl, in
    order. We use this to verify which turns ended up in which file."""
    if not path.exists():
        return []
    out: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "user":
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        out.append(block.get("text", ""))
    return out


async def run_turn(
    *,
    cwd: str,
    prompt: str,
    resume: str | None,
    fork: bool,
) -> str:
    """Run one turn through claude_agent_sdk, return the discovered session_id."""
    options = ClaudeAgentOptions(
        cwd=cwd,
        permission_mode="bypassPermissions",
        resume=resume,
        fork_session=fork,
        setting_sources=[],
    )
    discovered: str | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, SystemMessage):
                data = getattr(msg, "data", None) or {}
                if isinstance(data, dict) and "session_id" in data:
                    discovered = data["session_id"]
            elif isinstance(msg, AssistantMessage):
                pass
            elif isinstance(msg, ResultMessage):
                rs = getattr(msg, "session_id", None)
                if rs:
                    discovered = rs
                break
    if not discovered:
        raise RuntimeError("no session_id discovered for this turn")
    return discovered


async def main() -> int:
    cwd = tempfile.mkdtemp(prefix="bc-fork-verify-")
    print(f"cwd: {cwd}")

    # --- Step 1: spawn fresh session S0 ----------------------------------
    print("\n[1/4] spawning fresh session S0 (turn 1)...")
    s0 = await run_turn(
        cwd=cwd,
        prompt="Reply with the single word: ALPHA",
        resume=None,
        fork=False,
    )
    s0_path = jsonl_path(cwd, s0)
    s0_lines_after_step1 = count_lines(s0_path)
    print(f"   S0 = {s0}")
    print(f"   S0 jsonl = {s0_path} ({s0_lines_after_step1} lines)")
    assert s0_path.exists(), "S0 jsonl was not written"

    # --- Step 2: fork S0 into F1 (turn 2) --------------------------------
    print("\n[2/4] forking S0 -> F1 (turn 2)...")
    f1 = await run_turn(
        cwd=cwd,
        prompt="Reply with the single word: BETA",
        resume=s0,
        fork=True,
    )
    f1_path = jsonl_path(cwd, f1)
    print(f"   F1 = {f1}")
    print(f"   F1 jsonl = {f1_path} ({count_lines(f1_path)} lines)")

    assert f1 != s0, f"FAIL: fork did not mint a new sid (got {f1!r} == S0)"
    assert f1_path.exists(), f"FAIL: F1 jsonl does not exist at {f1_path}"

    s0_lines_after_step2 = count_lines(s0_path)
    assert s0_lines_after_step2 == s0_lines_after_step1, (
        f"FAIL: S0 jsonl grew from {s0_lines_after_step1} to "
        f"{s0_lines_after_step2} after forking — fork mutated parent."
    )

    # --- Step 3: resume F1 (turn 3) --------------------------------------
    print("\n[3/4] resuming F1 (turn 3, no fork)...")
    f1_lines_before_step3 = count_lines(f1_path)
    f1_again = await run_turn(
        cwd=cwd,
        prompt="Reply with the single word: GAMMA",
        resume=f1,
        fork=False,
    )
    f1_lines_after_step3 = count_lines(f1_path)
    print(f"   resume sid = {f1_again}")
    print(f"   F1 jsonl now {f1_lines_after_step3} lines (was {f1_lines_before_step3})")

    assert f1_again == f1, (
        f"FAIL: resume(F1, fork=False) returned a different sid {f1_again!r}; "
        f"expected to stay on {f1!r}"
    )
    assert f1_lines_after_step3 > f1_lines_before_step3, (
        f"FAIL: F1 jsonl did not grow on resume — was "
        f"{f1_lines_before_step3}, now {f1_lines_after_step3}"
    )

    # --- Step 4: resume F1 again (turn 4) --------------------------------
    print("\n[4/4] resuming F1 again (turn 4, no fork)...")
    f1_lines_before_step4 = f1_lines_after_step3
    f1_again2 = await run_turn(
        cwd=cwd,
        prompt="Reply with the single word: DELTA",
        resume=f1,
        fork=False,
    )
    f1_lines_after_step4 = count_lines(f1_path)
    print(f"   resume sid = {f1_again2}")
    print(f"   F1 jsonl now {f1_lines_after_step4} lines (was {f1_lines_before_step4})")

    assert f1_again2 == f1, (
        f"FAIL: second resume(F1) returned different sid {f1_again2!r}"
    )
    assert f1_lines_after_step4 > f1_lines_before_step4, (
        f"FAIL: F1 jsonl did not grow on second resume"
    )

    # --- Final S0 check: must still be untouched -------------------------
    s0_lines_final = count_lines(s0_path)
    assert s0_lines_final == s0_lines_after_step1, (
        f"FAIL: S0 jsonl was mutated by F1 resumes — was "
        f"{s0_lines_after_step1}, now {s0_lines_final}"
    )

    # --- Content sanity: F1 should show all of BETA/GAMMA/DELTA ----------
    f1_users = read_user_messages(f1_path)
    print(f"\nF1 user messages observed: {f1_users}")
    for needle in ("BETA", "GAMMA", "DELTA"):
        assert any(needle in u for u in f1_users), (
            f"FAIL: F1 jsonl missing user message containing {needle!r}; "
            f"saw {f1_users!r}"
        )

    print("\nALL ASSERTIONS PASSED — fork-once-then-resume mechanic is sound.")
    print(f"S0 stayed at {s0_lines_after_step1} lines, F1 grew to {f1_lines_after_step4}.")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except AssertionError as e:
        print(f"\n!!! VERIFICATION FAILED !!!\n{e}", file=sys.stderr)
        rc = 1
    except Exception as e:
        print(f"\n!!! UNEXPECTED ERROR !!!\n{type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        rc = 2
    sys.exit(rc)
