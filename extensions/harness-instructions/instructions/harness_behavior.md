# Better Agent Harness Behavior

Better Agent requires a parent-session reply after subagent work. If you call spawn_agent, multi_agent_v1.spawn_agent, or any other native subagent tool, then after every wait_agent result you must write your own final assistant message to the user. Do not end the turn with the wait_agent tool result as the last item.

Better Agent groups action/tool blocks under the assistant text that immediately precedes them.

When you are about to run tools, spawn subagents, inspect files, execute commands, or make edits, first write a concise assistant text block that states what you are doing and why. Then perform the consecutive tool/action calls. When that action group is done and you are moving to the next phase, write a new concise assistant text block; Better Agent will auto-collapse the previous action group under its lead-in text.

Keep these lead-in text blocks short and specific. Do not end a turn with only a tool result when user-facing work remains to be reported.
