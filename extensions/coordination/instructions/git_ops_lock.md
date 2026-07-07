# Coordination lock guidance

Before editing files in multi-agent work, call `lock_ops` with exact file locks:
`keys=["file_edit:<absolute-path>", ...]`. Do not include the repo-scoped
`git_ops:<absolute-repo-root>` key for ordinary file edits; that would serialize
unrelated writers and waste agent time. `file_edit:` locks are exact-key locks,
not recursive path locks, so lock every exact file path you may mutate.

Before mutating git/index/worktree state that is unsafe in a multi-agent
worktree, call `lock_ops` with a repo-scoped key:
`key="git_ops:<absolute-repo-root>"`. Use the canonical repository root from
`git rev-parse --show-toplevel`, not the current subdirectory. Hold this lock
only around the actual git/index-mutating operation (for example add, commit,
reset, checkout, rebase, or merge), then release it immediately with
`release=true` and the returned `holder_token`. If the current runtime does not
expose `lock_ops`, proceed with precise git operations instead of leaving work
uncommitted.

If `lock_ops` had to wait, use the precise `waited_keys` / `blocked_keys` in the
response. Re-read files only when their own `file_edit:` key was contended or
when their mtime/hash changed while waiting. If only `git_ops:<absolute-repo-root>`
was contended, re-check `git status`/`git diff` and your git plan, but do not
reread unrelated files solely because the git lock was busy. If the new state
conflicts with your intended change, flag it to the user instead of overwriting.
