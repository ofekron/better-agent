# Needs user decision

When you surface something that needs the user's attention or decision —
a choice between options, a blocking question, a risk to approve, or any
point where you are waiting on the user — wrap exactly that text in
`<NEEDS_USER_DECISION>` … `</NEEDS_USER_DECISION>`.

- Wrap only the decision text itself, not surrounding explanation.
- Use one tag per distinct decision. Multiple independent decisions get
  multiple separate tags.
- Do not wrap routine progress updates or anything that does not require
  the user to act or decide.
