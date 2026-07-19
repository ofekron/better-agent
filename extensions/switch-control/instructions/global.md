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
- Before promoting a change for end-to-end testing, verify that no TestApe or
  QA session is actively running against `qa`. Then fast-forward `qa` to the
  selected `dev` commit. Never create a merge commit, cherry-pick onto `qa`,
  or commit directly on `qa`. TestApe (`test_ui`) provisions and drives its
  own isolated instance from the pinned `qa` checkout. The active-checkout
  pointer and the user's running stack are irrelevant to `test_ui`; never
  switch or restart them for TestApe validation. Do not move `qa` while a
  TestApe or QA session is actively running against it.
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

The dev, qa, and main backend+frontend stacks run as separate line instances
with separate Better Agent homes and ports. The Line Switch control moves the
UI to the selected line's URL. Legacy single-stack switches may still use
`active_checkout.json`; do not edit that pointer file by hand.
