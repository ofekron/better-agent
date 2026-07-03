[$context_message. You are now in a fresh subprocess of the same Better Agent session — your prior context is not in this window. If you need earlier detail to continue, gather it yourself using your tools (read the session transcript, search files, or spawn a subagent) rather than assuming it.]

Better Agent session id: $app_session_id
Better Agent session file path: $app_session_file_path
$provider_session_ids_block
$provider_session_paths_block

Use `query_provider_native_transcript_index` when you need provider-native transcript history. Query `native_element_fts` for `text MATCH ...`; query `native_element_meta` for metadata filters or recency ordering by `sid`, `path`, `cwd`, `element_kind`, `role`, and `ts_utc`, then join back to `native_element_fts` by `rowid` when you need text. Use `ts_utc` for chronological ordering and add `LIMIT` in SQL when you want a bounded projection.

Better Agent ids are not always provider-native ids. To get native ids from a Better Agent session, read the Better Agent session JSON above and use `agent_session_id` for the primary provider session, `supervisor_agent_session_id` for supervisor history, and message-level `agent_session_id` fields for specific assistant turns. The previous provider session ids listed above are already native ids and can be used directly as `native_element_fts.sid`.

$prompt
