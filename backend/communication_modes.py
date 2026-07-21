from __future__ import annotations

ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN = "wait_and_grab_last_assistant_mssg_in_turn"
ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC = "continue_and_expect_inbox_back_async"

ASK_MODES = frozenset({
    ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
    ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC,
})

DEFAULT_ASK_MODE = ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN


def normalize_ask_mode(value: object) -> str:
    mode = str(value or DEFAULT_ASK_MODE).strip() or DEFAULT_ASK_MODE
    if mode not in ASK_MODES:
        raise ValueError(
            "mode must be 'wait_and_grab_last_assistant_mssg_in_turn' or "
            "'continue_and_expect_inbox_back_async'"
        )
    return mode
