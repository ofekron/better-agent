from __future__ import annotations

import pytest

from communication_modes import (
    ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC,
    ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
    DEFAULT_ASK_MODE,
    append_ask_response_contract,
    ask_response_contract,
    normalize_ask_execution,
    normalize_ask_mode,
)

SYNC = ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN
ASYNC = ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC


class TestNormalizeAskMode:
    def test_none_falls_back_to_default(self) -> None:
        assert normalize_ask_mode(None) == DEFAULT_ASK_MODE

    def test_empty_falls_back_to_default(self) -> None:
        assert normalize_ask_mode("") == DEFAULT_ASK_MODE

    def test_whitespace_falls_back_to_default(self) -> None:
        assert normalize_ask_mode("   ") == DEFAULT_ASK_MODE

    def test_sync_passthrough(self) -> None:
        assert normalize_ask_mode(SYNC) == SYNC

    def test_async_passthrough(self) -> None:
        assert normalize_ask_mode(ASYNC) == ASYNC

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            normalize_ask_mode("bogus")
        assert "mode must be" in str(exc.value)


class TestNormalizeAskExecution:
    def test_default_run_mode_is_direct(self) -> None:
        mode, run = normalize_ask_execution(SYNC, None)
        assert mode == SYNC
        assert run == "direct"

    def test_empty_run_mode_becomes_direct(self) -> None:
        _, run = normalize_ask_execution(SYNC, "")
        assert run == "direct"

    def test_fork_with_sync_allowed(self) -> None:
        mode, run = normalize_ask_execution(SYNC, "fork")
        assert (mode, run) == (SYNC, "fork")

    def test_invalid_run_mode_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            normalize_ask_execution(SYNC, "batch")
        assert "run_mode must be" in str(exc.value)

    def test_async_with_fork_rejected(self) -> None:
        with pytest.raises(ValueError) as exc:
            normalize_ask_execution(ASYNC, "fork")
        assert "async ask mode requires run_mode='direct'" in str(exc.value)


class TestAskResponseContract:
    def test_sync_contract_instructs_inline_reply(self) -> None:
        contract = ask_response_contract(SYNC)
        assert "<response_contract>" in contract
        assert "ask tool captures" in contract

    def test_async_without_sender_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            ask_response_contract(ASYNC)
        assert "sender_session_id is required" in str(exc.value)

    def test_async_empty_sender_raises(self) -> None:
        with pytest.raises(ValueError):
            ask_response_contract(ASYNC, sender_session_id="   ")

    def test_async_contract_references_inbox_and_sender(self) -> None:
        contract = ask_response_contract(ASYNC, sender_session_id="sess-42")
        assert "inbox(recipient_session_id=" in contract
        assert "sess-42" in contract

    def test_async_contract_html_escapes_sender(self) -> None:
        # A sender containing quote/html chars must be escaped so the injected
        # contract string cannot break out of the attribute / inject markup.
        malicious = '"><img src=x onerror=alert(1)>'
        contract = ask_response_contract(ASYNC, sender_session_id=malicious)
        assert malicious not in contract
        assert "&quot;" in contract
        assert "&gt;" in contract


class TestAppendAskResponseContract:
    def test_appends_sync_contract(self) -> None:
        out = append_ask_response_contract("do thing", SYNC)
        assert out.startswith("do thing")
        assert "<response_contract>" in out

    def test_appends_async_contract(self) -> None:
        out = append_ask_response_contract("do thing", ASYNC, sender_session_id="s1")
        assert "do thing" in out
        assert "s1" in out
