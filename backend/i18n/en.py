TRANSLATIONS: dict[str, str] = {
    # --- HTTP / REST errors ---
    "error.session_not_found": "Session not found",
    "error.provider_not_found": "provider not found",
    "error.cannot_delete_default_provider": "cannot delete the default provider — set another as default first",
    "error.name_required": "name required",
    "error.no_default_provider": "no default provider",
    "error.invalid_path": "Invalid path",
    "error.orchestration_mode_frozen": "orchestration_mode is frozen after session creation",
    "error.no_recognized_selector": "no recognized selector field",
    "error.provider_change_during_active_run": "cannot change provider while a turn is in flight — stop or wait for the current turn to finish",
    "error.session_not_found_retry": "session not found",
    "error.message_id_required": "message_id required",
    "error.assistant_message_id_required": "assistant_message_id required",
    "error.assistant_message_not_found": "assistant message not found",
    "error.no_preceding_user_message": "no preceding user message — nothing to retry",
    "error.prompt_required": "prompt is required",
    "error.fork_no_claude_session": "parent session has no claude session id yet — take at least one turn before forking",
    "error.mode_must_be_fork_or_new": "mode must be 'fork' or 'new'",
    "error.parent_session_not_found": "parent session not found",
    "error.no_live_eng_session": "no live engineering session for this parent",
    "error.not_eng_session": "not a prompt-engineering session",
    "error.temp_file_missing": "temp file missing",
    "error.file_path_required": "file_path is required",
    "error.no_live_file_editor": "no live file-editor session for this file",
    "error.not_file_editor_session": "not a file-editor session",
    "error.missing_invalid_field": "missing/invalid field: {e}",
    "error.draft_input_must_be_string": "draft_input must be a string",
    "error.client_seq_must_be_number": "client_seq must be a number",
    "error.invalid_internal_token": "Invalid internal token",
    "error.file_panel_path_required": "file panel `path` is required",
    "error.file_panels_list_required": "`panels` must be a list",
    "error.image_not_found": "Image not found",
    "error.approval_not_found": "approval not found",
    "error.approval_expired": "approval expired",
    "error.node_request_not_found": "node registration request not found",
    "error.node_request_expired": "node registration request expired",
    "error.cwd_required": "cwd required",
    "error.orchestration_mode_must_be_manager_or_native": "orchestration_mode must be team|native",
    "error.init_turn_failed": "init turn failed: {e}",
    "error.init_turn_no_session_id": "init turn produced no session id (likely cancelled)",
    "error.session_already_initializing": "session is already being initialized",
    "error.cwd_plus_session_id_required": "cwd + agent_session_id required",
    "error.bc_session_not_found": "Better Agent session not found",
    "error.bc_session_cwd_mismatch": "Better Agent session cwd ({agent_cwd}) does not match {cwd}",
    "error.bc_session_no_agent_sid": "Better Agent session has no agent_sid in {mode} mode yet — open it and send one prompt to initialize before marking as worker",

    # --- WebSocket errors ---
    "error.ws_invalid_json": "Invalid JSON",
    "error.ws_empty_prompt": "Empty prompt",
    "error.ws_no_session_selected": "No session selected",
    "error.ws_session_not_found": "Session not found",
    "error.ws_no_active_turn_to_stop": "No active turn to stop",
    "error.ws_no_active_turn_to_steer": "No active turn to steer",
    "error.ws_active_turn_no_steering": "Active turn does not support steering",
    "error.ws_no_queued_prompt": "No queued prompt to promote",
    "error.ws_review_supervisor_only": "Review only available when the supervisor toggle is on",
    "error.ws_session_busy": "Session is busy",

    # --- Rename ---
    "error.name_is_required_rename": "name is required",
    "error.session_not_found_rename": "session not found",

    # --- Traces ---
    "error.traces_not_found": "No traces found",
    "error.trace_not_found": "Trace not found",

    # --- Delegation ---
    "delegation.no_active_turn": "Cannot request a fresh worker without an active turn. Resume an existing worker from <known_workers>.",
    "delegation.nested_no_fresh_workers": "Nested delegations cannot create fresh workers. Resume an existing worker from <known_workers> or stop and let the user delegate from the top level.",
    "delegation.justification_required": "`justification` is required when requesting a fresh worker. Explain in one or two sentences why no existing worker fits.",
    "delegation.orchestration_mode_required": "`orchestration_mode` is required when requesting a fresh worker (must be 'team' or 'native').",
    "delegation.user_denied_creation": "User denied creation of a fresh worker. Resume an existing worker from <known_workers>, self-review the prior delegation differently (e.g. Grep the worker's jsonl for errors), or stop.",
    "delegation.worker_not_registered": "Worker {worker_session_id} is not registered. Check the workers panel.",
    "delegation.worker_bc_deleted": "Worker Better Agent session {worker_session_id} no longer exists. It has been unregistered.",
    "delegation.worker_no_claude_session": "Worker Better Agent session {worker_session_id} has no underlying claude session yet — it has never taken a turn in {mode} mode. Open it in the UI and send one prompt to initialize it before delegating.",
    "delegation.instructions_worker_description_required": "Error: 'instructions' and 'worker_description' are required.",
    "delegation.cancelled": "cancelled",
    "delegation.user_denied_fresh_worker": "User denied this fresh-worker request.",

    # --- Approval ---
    "approval.init_failed": "Failed to initialize new worker: {e}",
    "approval.init_no_session_id": "init turn produced no claude session id (likely cancelled)",

    # --- Session defaults ---
    "session.default_name": "Session {time}",
    "session.fork_suffix": " (fork)",
    "session.fork_suffix_n": " (fork {n})",
    "session.untitled": "Untitled",
    "session.untitled_worker": "(untitled)",

    # --- Prompt engineer ---
    "prompt_engineer.name_fork": "Engineer — {parent_name}",
    "prompt_engineer.name_fresh": "Engineer — fresh",
    "prompt_engineer.parent_no_claude_session": "parent session has no claude session id yet — take at least one turn before forking",
    "prompt_engineer.parent_cwd_invalid": "parent session cwd is not a usable directory: {cwd}",

    # --- Supervisor ---
    "supervisor.verdict_failed_message": "Supervisor verdict failed (see backend log) — letting the worker stop.",
    "supervisor.verdict_capped_message": "Supervisor reached the {max}-verdict cap; letting the worker stop to avoid an infinite loop.",

    # --- Runner ---
    "runner.invalid_mode": "invalid mode: {mode}",
    "runner.missing_fields": "missing required fields: prompt and/or cwd",
    "runner.manager_mode_missing_fields": "manager mode requires app_session_id, backend_url, internal_token",
    "runner.delegate_http_error": "delegate HTTP {code}: {reason} {body}",
    "runner.delegate_url_error": "delegate URL error: {reason}",
    "runner.delegate_general_error": "Delegate error: {e}",
    "runner.delegate_non_json": "delegate: non-json response: {e}: {raw}",
    "runner.browser_harness_non_json": "browser-harness: non-json response: {e}: {raw}",
    "runner.open_file_panel_non_json": "open-file-panel: non-json response: {e}: {raw}",
    "runner.cancelled": "cancelled",
    "runner.failed_read_input": "failed to read input.json: {e}",

    # --- Orchestrator ---
    "orchestrator.session_not_found": "Session not found",
    "orchestrator.message_not_found": "Message not found",
    "orchestrator.message_no_claude_uuid": "Message has no agent_message_uuid",
    "orchestrator.rewind_not_supported": "Provider does not support rewind",
    "orchestrator.session_no_sid_field": "Session has no {sid_field}",

    # --- Workers ---
    "worker.default_name": "Worker",
    "workers.untitled": "(untitled)",
}
