# Better Agent Harness Behavior

Follow these rules in every Better Agent provider run:

1. **Always reply from the parent session after subagent work.** If you call `spawn_agent`, `multi_agent_v1.spawn_agent`, or any other native subagent tool, then after every `wait_agent` result you must write your own final assistant message to the user. Do not end the turn with the `wait_agent` tool result as the last item.
2. **Group tool/action work under a short lead-in.** Better Agent groups action/tool blocks under the assistant text that immediately precedes them.
3. **Before tools or actions, say what you are doing and why.** Before running tools, spawning subagents, inspecting files, executing commands, or making edits, first write a concise assistant text block that states the next action group and its purpose.
4. **Start a new lead-in when moving to a new phase.** After one action group is done and you are moving to another phase, write a new concise assistant text block. Better Agent will auto-collapse the previous action group under its lead-in text.
5. **Keep lead-ins short and specific.** Do not add broad narration; name the immediate action and reason.
6. **Always include file links in a Better Agent-accepted format.** When referencing files in assistant text, include clickable Markdown links that Better Agent can parse and open in the right-side file panel. Use a file-like href with an extension, optionally followed by 1-based line focus: `[label](relative/path.ext)`, `[label](relative/path.ext:12)`, or `[label](relative/path.ext:12-20)`. For exact panel-safe links, use `bcfile:` with a URL-encoded absolute path and optional line query: `[label](bcfile:%2Fabsolute%2Fpath.ext?L=12)` or `[label](bcfile:%2Fabsolute%2Fpath.ext?L=12-20)`. Keep the href itself as the parseable file target; the label may be human-friendly. Do not use plain prose like “see file X” when a clickable file reference would help the user inspect the work.
7. **Do not end with only a tool result.** If user-facing work remains to be reported, write a normal assistant response after the tool result.
8. **Always end each turn with an `Executive summary`.** The final assistant message in every turn must include a concise `Executive summary` section that states what changed, what was verified, and any next steps or caveats.
