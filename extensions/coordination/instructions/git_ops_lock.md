# Coordination Git Ops Lock

When local changes exist that you did not make, assume other agents may be working in parallel.

Before mutating git operations that are unsafe in a multi-agent worktree, call `lock_ops` with a repo-scoped key: `key="git_ops:<absolute-repo-root>"`. Use the canonical repository root from `git rev-parse --show-toplevel`, not the current subdirectory. Keep the returned `holder_token` when the `lock_ops` tool is available in the current runtime. Release it as soon as the git operation finishes by calling `lock_ops` again with the same repo-scoped key, `release=true`, and that `holder_token`. If the current runtime does not expose `lock_ops`, proceed with precise git operations instead of leaving work uncommitted.

Unsafe git operations include branch/index/remote mutations such as `git add`, `git commit`, `git push`, `git pull`, `git fetch --prune`, `git merge`, `git rebase`, `git checkout`, `git switch`, `git reset`, `git restore`, `git clean`, and stash operations. Read-only git inspection such as `git status`, `git diff`, `git log`, `git show`, and `git worktree list` does not need the lock.

Couple this lock with file editing: whenever you acquire `file_edit:` locks to edit files, acquire this repo-scoped `git_ops` key in the same `lock_ops` call and hold it across the edit/commit phase, so a concurrent git operation cannot mutate the tree while you edit.

If `lock_ops` does not grant the lock immediately — the response has `waited: true`, or a single-key acquire first came back with `error: "locked"`/`"timeout"` — another agent just held this repo's git or files, so the working tree has probably changed underneath you. Before proceeding, re-inspect state (`git status`/`git diff` and re-read the files you rely on) and re-validate your plan against it; if the change conflicts with what you were about to do, flag it to the user instead of overwriting.
