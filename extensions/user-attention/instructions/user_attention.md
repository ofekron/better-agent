# User attention

Use these tags as machine-readable status signals, not as decoration. Wrap only
the exact user-facing sentence that should create the marker; the wrapper is
stripped from the rendered message, and the marker clears when the user views the
session.

Only emit a marker when the final state of the turn is clear. Do not tag
progress updates, intermediate notes, or speculative text.

## Needs a decision (orange dot)

Use this only when the turn cannot continue correctly without the user choosing,
approving risk, supplying missing information, or resolving a conflict. Wrap the
single sentence that states the required action in
`<NEEDS_USER_DECISION>` ... `</NEEDS_USER_DECISION>`.

- Do not use it for FYI warnings, caveats, status updates, or optional follow-ups.
- Use one tag per distinct decision. Multiple independent decisions get
  multiple separate tags.
- If work can continue without the answer, do not tag it.
- If you use this tag, do not also emit `<ALL_TASKS__DONE>` in the same turn.

Good:

`<NEEDS_USER_DECISION>Choose whether to remove the stale config or keep it for manual migration.</NEEDS_USER_DECISION>`

Bad:

`<NEEDS_USER_DECISION>I noticed a possible issue, and I can look later.</NEEDS_USER_DECISION>`

## All tasks done (blue dot)

Use this only at the end of the final assistant message when every requested task
is complete, required verification is done or explicitly reported, resources you
opened are closed or listed, and no user decision is pending. Wrap one short
completion sentence in `<ALL_TASKS__DONE>` ... `</ALL_TASKS__DONE>`.

- Put it after the recap/TLDR, as the last line of the message.
- Emit it at most once per turn.
- Do not emit it after partial work, failed required verification, an open
  blocker, or a question that needs a user answer.
- Do not wrap the whole final answer; wrap only the one-line completion signal.

Good:

`<ALL_TASKS__DONE>All requested work is complete.</ALL_TASKS__DONE>`

Bad:

`<ALL_TASKS__DONE>Implemented the change, but tests are still running.</ALL_TASKS__DONE>`
