"""Git-control policy: which sessions may run git.

`git_policy` is an OPTIONAL, extension-managed field on the session record. The
git-control extension (private) stamps it: `"locked"` on normal sessions,
`"worker"` on the single blessed git-worker. ABSENT (the default, before the
extension is active) ⇒ no enforcement — current behavior is preserved. So this
module is INERT until the extension arms it; shipping it alone changes nothing.

Enforcement is provider-specific (only BA-controlled exec paths can be hardened):
- Claude: `claude_disallowed_extra()` → `"Bash(git:*)"` appended to the run's
  `disallowed_tools`. Verified honored even under `bypassPermissions`
  (live probe 2026-06-28).
- OpenAI/agy: `command_runs_git()` regex at the BA `_tool_bash` exec gate
  (`runner_better_agent.py`). Provider runner reads `git_policy` from its run input.
- Gemini/Codex: CLI-internal exec, NOT enforceable → audit-only (handled by the
  extension), not here.
"""

from __future__ import annotations

import re
from typing import Optional

LOCKED = "locked"
WORKER = "worker"

# Claude disallowed_tools entries that block direct git shell access for a
# locked session. `git` and `gh` binaries; git-library use via python/node is a
# documented residual hole (editors / libgit2 class), not closable at this layer.
CLAUDE_DENY_TOOLS = ["Bash(git:*)", "Bash(gh:*)"]


def is_worker(sess_rec: Optional[dict]) -> bool:
    return bool(sess_rec) and sess_rec.get("git_policy") == WORKER


def is_locked(sess_rec: Optional[dict]) -> bool:
    return bool(sess_rec) and sess_rec.get("git_policy") == LOCKED


def claude_disallowed_extra(sess_rec: Optional[dict]) -> list[str]:
    """Extra `disallowed_tools` for a Claude run: git deny iff this session is
    explicitly locked. Worker and unarmed (absent) sessions get nothing."""
    return list(CLAUDE_DENY_TOOLS) if is_locked(sess_rec) else []


# Exhaustive direct-git deny for BA-owned exec paths (OpenAI `_tool_bash`).
# Matches the git/gh binaries and common git-library invocations, anchored so a
# leading token or a shell separator precedes the name (avoids "legit" substrings).
_GIT_RE = re.compile(
    r"(?:^|[\s;&|`$(><\"'])\s*"
    r"(?:git|gh|pygit2|dulwich|GitPython|isomorphic-?git|simple-?git)\b",
    re.IGNORECASE,
)


def command_runs_git(command: str) -> bool:
    """True if a shell command string invokes git/gh or a git library. Used at
    BA-owned exec gates to deny git for locked sessions."""
    return bool(_GIT_RE.search(command or ""))
