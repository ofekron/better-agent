# Development lines (dev/qa/main worktrees)

Better Agent development runs on three pinned worktrees, one per line: `dev`
(the default working line), a sibling `<repo>-qa` checkout pinned to `qa`,
and a sibling `<repo>-main` checkout pinned to `main`. All feature work
happens on `dev`. Never develop directly inside the `qa` or `main`
checkouts; those lines only receive merges promoted from the line below
them.

Flow: `dev` → `qa` → `main`.

- Develop and iterate on `dev`.
- When a change is ready to be tested end-to-end, merge/promote `dev` into
  `qa` and switch the running stack to `qa`. TestApe (`test_ui`) must target
  `qa`, not `dev` — switch the active checkout to `qa` before running UI
  tests, since TestApe just drives whatever line is currently active.
- Promotion from `qa` to `main` happens ONLY on the user's explicit request.
  Never merge into `main` or switch the running stack to `main`
  autonomously, even after `qa` testing passes — wait to be asked.

The running backend+frontend follow the active-checkout pointer
(`active_checkout.json` under the Better Agent state home), switched from the
UI (or the switch-control capability). Do not edit the pointer file by hand;
use the Line Switch control or ask the user. If a switch is in flight
(pointer status `switching`), do not start work that depends on backend
availability until it completes.
