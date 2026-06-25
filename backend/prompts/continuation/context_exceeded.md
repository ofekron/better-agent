[$context_message. $continuity_message If you need earlier detail to continue, gather it yourself using your tools (read the session transcript, search files, or spawn a subagent) rather than assuming it.]

Better Agent session id: $app_session_id
Better Agent session file path: $app_session_file_path
$provider_session_ids_block
$provider_session_paths_block

Use the `get-requirements` skill for requirement memory: call `fire_get_requirements` early with the concrete task, continue safe independent work while it runs in the background, then call `get_requirements_results` right before the requirements are needed and wait if necessary. Do not query the provider-native transcript SQL index directly from this session; that raw SQL tool is reserved for the provisioned get-requirements processor worker.

Better Agent ids are not always provider-native ids. To get native ids from a Better Agent session, read the Better Agent session JSON above and use `agent_session_id` for the primary provider session, `supervisor_agent_session_id` for supervisor history, and message-level `agent_session_id` fields for specific assistant turns. The previous provider session ids listed above are already native ids.

$prompt
