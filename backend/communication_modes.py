from __future__ import annotations

from html import escape

ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN = "wait_and_grab_last_assistant_mssg_in_turn"
ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC = "continue_and_expect_inbox_back_async"

ASK_MODES = frozenset({
    ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
    ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC,
})

DEFAULT_ASK_MODE = ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN
ASK_RUN_MODES = frozenset({"direct", "fork"})

IN_TURN_REPLY_INSTRUCTION = (
    "When you need the target's answer inline in this same turn, use "
    f"ask(mode='{ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN}'). "
    "Use mssg only for one-way coordination; mssg never waits for or returns a reply."
)


def normalize_ask_mode(value: object) -> str:
    mode = str(value or DEFAULT_ASK_MODE).strip() or DEFAULT_ASK_MODE
    if mode not in ASK_MODES:
        raise ValueError(
            "mode must be 'wait_and_grab_last_assistant_mssg_in_turn' or "
            "'continue_and_expect_inbox_back_async'"
        )
    return mode


def normalize_ask_execution(mode: object, run_mode: object) -> tuple[str, str]:
    normalized_mode = normalize_ask_mode(mode)
    normalized_run_mode = str(run_mode or "direct").strip() or "direct"
    if normalized_run_mode not in ASK_RUN_MODES:
        raise ValueError("run_mode must be 'direct' or 'fork'")
    if (
        normalized_mode == ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC
        and normalized_run_mode == "fork"
    ):
        raise ValueError("async ask mode requires run_mode='direct'")
    return normalized_mode, normalized_run_mode


def ask_response_contract(
    mode: object,
    *,
    sender_session_id: str = "",
) -> str:
    normalized = normalize_ask_mode(mode)
    if normalized == ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN:
        return (
            "<response_contract>\n"
            "The sender is waiting for this turn to finish. Put the final result "
            "in your assistant response for this turn. The ask tool captures your "
            "last assistant text batch and returns it inline to the sender as the "
            "tool result. Do not call mssg or inbox to return the final result.\n"
            "</response_contract>"
        )

    sender = str(sender_session_id or "").strip()
    if not sender:
        raise ValueError("sender_session_id is required for asynchronous ask mode")
    return (
        "<response_contract>\n"
        "This ask is asynchronous; the sender is not waiting for this turn. "
        "When the task is complete, call "
        f'inbox(recipient_session_id="{escape(sender, quote=True)}", '
        "message=<final result>) exactly once. Use inbox for the final result "
        "because this incoming message is asynchronous. Do not use mssg for "
        "the final result.\n"
        "</response_contract>"
    )


def append_ask_response_contract(
    message: str,
    mode: object,
    *,
    sender_session_id: str = "",
) -> str:
    return (
        f"{message}\n\n"
        f"{ask_response_contract(mode, sender_session_id=sender_session_id)}"
    )
