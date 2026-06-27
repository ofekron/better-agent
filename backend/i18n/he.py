TRANSLATIONS: dict[str, str] = {
    # --- HTTP / REST errors ---
    "error.session_not_found": "סשן לא נמצא",
    "error.provider_not_found": "ספק לא נמצא",
    "error.cannot_delete_default_provider": "לא ניתן למחוק את ספק ברירת המחדל — קבעו ספק אחר כברירת מחדל קודם",
    "error.name_required": "נדרש שם",
    "error.no_default_provider": "אין ספק ברירת מחדל",
    "error.invalid_path": "נתיב לא תקין",
    "error.orchestration_mode_frozen": "מצב תזמור קפוא לאחר יצירת הסשן",
    "error.no_recognized_selector": "לא זוהה שדה בורר",
    "error.provider_change_during_active_run": "לא ניתן להחליף ספק בזמן ריצת תור — עצור או המתן לסיום התור הנוכחי",
    "error.session_not_found_retry": "סשן לא נמצא",
    "error.message_id_required": "נדרש message_id",
    "error.assistant_message_id_required": "נדרש assistant_message_id",
    "error.assistant_message_not_found": "הודעת עוזר לא נמצאה",
    "error.no_preceding_user_message": "אין הודעת משתמש קודמת — אין מה לנסות שוב",
    "error.prompt_required": "נדרש פרומפט",
    "error.fork_no_claude_session": "לסשן ההורה אין מזהה סשן claude עדיין — בצעו לפחות תור אחד לפני פיצול",
    "error.mode_must_be_fork_or_new": "המצב חייב להיות 'fork' או 'new'",
    "error.parent_session_not_found": "סשן הורה לא נמצא",
    "error.no_live_eng_session": "אין סשן הנדסה פעיל להורה זה",
    "error.not_eng_session": "לא סשן הנדסת פרומפטים",
    "error.temp_file_missing": "קובץ זמני חסר",
    "error.file_path_required": "נדרש file_path",
    "error.no_live_file_editor": "אין סשן עריכת קבצים פעיל לקובץ זה",
    "error.not_file_editor_session": "לא סשן עריכת קבצים",
    "error.missing_invalid_field": "שדה חסר/לא תקין: {e}",
    "error.draft_input_must_be_string": "draft_input חייב להיות מחרוזת",
    "error.client_seq_must_be_number": "client_seq חייב להיות מספר",
    "error.invalid_internal_token": "טוקן פנימי לא תקין",
    "error.file_panel_path_required": "נדרש `path` ללוח קובץ",
    "error.file_panels_list_required": "`panels` חייב להיות רשימה",
    "error.image_not_found": "תמונה לא נמצאה",
    "error.approval_not_found": "אישור לא נמצא",
    "error.approval_expired": "אישור פג תוקף",
    "error.node_request_not_found": "בקשת רישום צומת לא נמצאה",
    "error.node_request_expired": "בקשת רישום צומת פגה",
    "error.cwd_required": "נדרש cwd",
    "error.orchestration_mode_must_be_manager_or_native": "orchestration_mode חייב להיות team או native",
    "error.init_turn_failed": "תור האתחול נכשל: {e}",
    "error.init_turn_no_session_id": "תור האתחול לא ייצר מזהה סשן (כנראה בוטל)",
    "error.session_already_initializing": "סשן כבר נמצא בתהליך אתחול",
    "error.cwd_plus_session_id_required": "נדרש cwd + agent_session_id",
    "error.bc_session_not_found": "סשן Better Agent לא נמצא",
    "error.bc_session_cwd_mismatch": "ה-cwd של סשן Better Agent ({agent_cwd}) לא תואם ל-{cwd}",
    "error.bc_session_no_agent_sid": "לסשן Better Agent אין agent_sid במצב {mode} עדיין — פתחו אותו ושלחו פרומפט אחד לאתחול לפני סימון כעובד",

    # --- WebSocket errors ---
    "error.ws_invalid_json": "JSON לא תקין",
    "error.ws_empty_prompt": "פרומפט ריק",
    "error.ws_no_session_selected": "לא נבחר סשן",
    "error.ws_session_not_found": "סשן לא נמצא",
    "error.ws_no_active_turn_to_stop": "אין תור פעיל לעצירה",
    "error.ws_no_active_turn_to_steer": "אין תור פעיל לכיוון",
    "error.ws_active_turn_no_steering": "התור הפעיל לא תומך בכיוון",
    "error.ws_no_queued_prompt": "אין פרומפט בתור לקידום",
    "error.ws_review_supervisor_only": "סקירה זמינה רק כשהמפקח מופעל",
    "error.ws_session_busy": "הסשן עסוק",

    # --- Rename ---
    "error.name_is_required_rename": "נדרש שם",
    "error.session_not_found_rename": "סשן לא נמצא",

    # --- Traces ---
    "error.traces_not_found": "לא נמצאו מעקבים",
    "error.trace_not_found": "מעקב לא נמצא",

    # --- Delegation ---
    "delegation.no_active_turn": "לא ניתן לבקש עובד חדש ללא תור פעיל. המשיכו עובד קיים מ-<known_workers>.",
    "delegation.nested_no_fresh_workers": "האצלות מקוננות לא יכולות ליצור עובדים חדשים. המשיכו עובד קיים מ-<known_workers> או עצרו ותנו למשתמש להאציל מהרמה העליונה.",
    "delegation.justification_required": "`justification` נדרש בבקשת עובד חדש. הסבירו במשפט אחד או שניים למה אף עובד קיים לא מתאים.",
    "delegation.orchestration_mode_required": "`orchestration_mode` נדרש בבקשת עובד חדש (חייב להיות 'team' או 'native').",
    "delegation.user_denied_creation": "המשתמש דחה יצירת עובד חדש. המשיכו עובד קיים מ-<known_workers>, סקרו מחדש את ההאצלה הקודמת (למשל Grep ב-jsonl של העובד לשגיאות), או עצרו.",
    "delegation.worker_bc_deleted": "סשן Better Agent של העובד {worker_session_id} כבר לא קיים. הרישום בוטל.",
    "delegation.worker_no_claude_session": "לסשן Better Agent של העובד {worker_session_id} אין סשן claude בסיסי עדיין — הוא מעולם לא ביצע תור במצב {mode}. פתחו אותו בממשק ושלחו פרומפט אחד לאתחול לפני האצלה.",
    "delegation.instructions_worker_description_required": "שגיאה: 'instructions' ו-'worker_description' נדרשים.",
    "delegation.cancelled": "בוטל",
    "delegation.user_denied_fresh_worker": "המשתמש דחה את בקשת העובד החדש.",

    # --- Approval ---
    "approval.init_failed": "נכשל לאתחל עובד חדש: {e}",
    "approval.init_no_session_id": "תור האתחול לא ייצר מזהה סשן (כנראה בוטל)",

    # --- Session defaults ---
    "session.default_name": "סשן {time}",
    "session.fork_suffix": " (פיצול)",
    "session.fork_suffix_n": " (פיצול {n})",
    "session.untitled": "ללא שם",
    "session.untitled_worker": "(ללא שם)",

    # --- Prompt engineer ---
    "prompt_engineer.name_fork": "הנדסה — {parent_name}",
    "prompt_engineer.name_fresh": "הנדסה — חדש",
    "prompt_engineer.parent_no_claude_session": "לסשן ההורה אין מזהה סשן claude עדיין — בצעו לפחות תור אחד לפני פיצול",
    "prompt_engineer.parent_cwd_invalid": "ה-cwd של סשן ההורה אינו תיקייה שמישה: {cwd}",

    # --- Supervisor ---
    "supervisor.verdict_failed_message": "פסק המפקח נכשל (ראו יומן backend) — מניחים לעובד לעצור.",
    "supervisor.verdict_capped_message": "המפקח הגיע למגבלת {max} פסקים; מניחים לעובד לעצור כדי למנוע לולאה אינסופית.",

    # --- Runner ---
    "runner.invalid_mode": "מצב לא תקין: {mode}",
    "runner.missing_fields": "שדות נדרשים חסרים: פרומפט ו/או cwd",
    "runner.manager_mode_missing_fields": "מצב מנהל דורש app_session_id, backend_url, internal_token",
    "runner.delegate_http_error": "האצלה HTTP {code}: {reason} {body}",
    "runner.delegate_url_error": "שגיאת URL בהאצלה: {reason}",
    "runner.delegate_general_error": "שגיאת האצלה: {e}",
    "runner.delegate_non_json": "האצלה: תגובה לא-JSON: {e}: {raw}",
    "runner.cancelled": "בוטל",
    "runner.failed_read_input": "נכשל לקרוא input.json: {e}",
    "runner.mssg_non_json": "mssg: תגובה שאינה JSON: {e}: {raw}",
    "runner.open_file_panel_non_json": "open-file-panel: תגובה שאינה JSON: {e}: {raw}",

    # --- Orchestrator ---
    "orchestrator.session_not_found": "סשן לא נמצא",
    "orchestrator.message_not_found": "הודעה לא נמצאה",
    "orchestrator.message_no_claude_uuid": "להודעה אין agent_message_uuid",
    "orchestrator.rewind_not_supported": "הספק לא תומך ב-rewind",
    "orchestrator.session_no_sid_field": "לסשן אין {sid_field}",

    # --- Workers ---
    "worker.default_name": "עובד",
    "workers.untitled": "(ללא שם)",
}
