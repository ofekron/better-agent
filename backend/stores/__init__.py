"""Persistence stores not owned by any single orchestration mode.

`worker_store` (global worker registry) and `pending_approvals` (the
fresh-worker approval queue) used to live inside `backend/orchs/manager/`,
which incorrectly implied they were manager-mode-internal. They are
not: workers are global, and the supervisor/future modes could also
spawn or delegate to them. Putting them under a single mode package
hides that.

Today only manager-mode code actively imports from here; the package
exists so that boundary is correct by structure, not by accident. If
you wire supervisor / native / a new mode to create workers or
approvals, import from `stores`, not from `orchs/<mode>/`.

Stores in this package own their on-disk layout under `ba_home()`.
"""
