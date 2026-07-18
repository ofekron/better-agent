# Development lines (dev/qa/main worktrees)

Better Agent development runs on three pinned worktrees, one per line: `dev`
(the default working line), a sibling `<repo>-qa` checkout pinned to `qa`,
and a sibling `<repo>-main` checkout pinned to `main`. All feature work
happens on `dev`. Never develop directly inside the `qa` or `main`
checkouts; those lines only receive fast-forward promotions from the line
below them.

Flow: `dev` → `qa` → `main`.

- Keep one ancestry chain at all times: `main` must be an ancestor of `qa`,
  and `qa` must be an ancestor of `dev`. `qa` may intentionally lag behind
  `dev` between promotions.
- Develop and iterate on `dev`.
- When a change is ready to be tested end-to-end, fast-forward `qa` to the
  selected `dev` commit and switch the running stack to `qa`. Never create a
  merge commit, cherry-pick onto `qa`, or commit directly on `qa`. TestApe
  (`test_ui`) must target `qa`, not `dev` — switch the active checkout to
  `qa` before running UI tests, since TestApe just drives whatever line is
  currently active. Do not move `qa` while a TestApe or QA session is
  actively running against it; verify the active checkout and test-session
  state first.
- Promotion from `qa` to `main` happens ONLY on the user's explicit request.
  Fast-forward `main` to `qa`; never create a merge commit, cherry-pick onto
  `main`, or commit directly on `main`. Never promote or switch the running
  stack to `main` autonomously, even after `qa` testing passes — wait to be
  asked.

Use fast-forward-only Git operations for normal promotion. If a fast-forward
is impossible, stop instead of creating a merge commit. Repair divergence only
as a coordinated operation: freeze writers, create and remotely verify
immutable backup refs, rebase `dev` onto `main`, reset `qa` to the current
`main` tip unless the user explicitly specifies another target, and update
shared refs with exact force-with-lease protection. Never rebase published
`main` or `qa`; rebase published `dev` only during this backed-up repair flow.

The running backend+frontend follow the active-checkout pointer
(`active_checkout.json` under the Better Agent state home), switched from the
UI (or the switch-control capability). Do not edit the pointer file by hand;
use the Line Switch control or ask the user. If a switch is in flight
(pointer status `switching`), do not start work that depends on backend
availability until it completes.
