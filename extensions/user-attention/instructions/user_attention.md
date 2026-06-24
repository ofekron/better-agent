# User attention

Mark a session for the user's attention with one of two tags. Each sets a
colored dot on the session that clears once the user views it.

## Needs a decision (orange dot)

When you surface something that needs the user's attention or decision —
a choice between options, a blocking question, a risk to approve, or any
point where you are waiting on the user — wrap exactly that text in
`<NEEDS_USER_DECISION>` … `</NEEDS_USER_DECISION>`.

- Wrap only the decision text itself, not surrounding explanation.
- Use one tag per distinct decision. Multiple independent decisions get
  multiple separate tags.
- Do not wrap routine progress updates or anything that does not require
  the user to act or decide.

## All tasks done (blue dot)

When you have completed everything the user asked for and nothing is left
pending, wrap your one-line completion confirmation in
`<ALL_TASKS__DONE>` … `</ALL_TASKS__DONE>`.

- Emit it only when every task is truly done — not on partial progress and
  not while still waiting on the user.
- Use it at most once, at the very end of your final message for the turn.
- Do not combine it with `<NEEDS_USER_DECISION>`: if you are still waiting
  on the user, the work is not done.
