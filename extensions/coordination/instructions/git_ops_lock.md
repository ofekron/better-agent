# Coordination Git Ops Lock

When local changes exist that you did not make, assume other agents may be working in parallel.

Before mutating git operations that are unsafe in a multi-agent worktree, call `lock_ops` with a repo-scoped key: `key="git_ops:<absolute-repo-root>"`. Use the canonical repository root from `git rev-parse --show-toplevel`, not the current subdirectory. Keep the returned `holder_token` when the `lock_ops` tool is available in the current runtime. Release it as soon as the git operation finishes by calling `lock_ops` again with the same repo-scoped key, `release=true`, and that `holder_token`. If the current runtime does not expose `lock_ops`, proceed with precise git operations instead of leaving work uncommitted.

Unsafe git operations include branch/index/remote mutations such as `git add`, `git commit`, `git push`, `git pull`, `git fetch --prune`, `git merge`, `git rebase`, `git checkout`, `git switch`, `git reset`, `git restore`, `git clean`, and stash operations. Read-only git inspection such as `git status`, `git diff`, `git log`, `git show`, and `git worktree list` does not need the lock.
