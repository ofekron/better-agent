# Development lines (main/dev worktrees)

Better Agent development runs on two pinned worktrees: the `dev` checkout is
the default working line; a sibling `<repo>-main` checkout is pinned to
`main`. Main-line work happens only inside the `-main` directory and only
when the user has switched the running stack to it; everything else is dev.
Never develop both lines in parallel.

The running backend+frontend follow the active-checkout pointer
(`active_checkout.json` under the Better Agent state home), switched from the
UI. Do not edit the pointer file by hand; use the Line Switch control or ask
the user. If a switch is in flight (pointer status `switching`), do not start
work that depends on backend availability until it completes.
