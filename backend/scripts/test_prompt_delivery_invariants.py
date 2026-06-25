from __future__ import annotations

import ast
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"missing function {name}")


def test_queue_record_survives_until_user_message_persist() -> None:
    processor = _function_source(BACKEND / "orchestrator.py", "_run_session_processor")
    before_handle = processor.split("await self.handle_prompt", 1)[0]
    removals = before_handle.count("session_manager.remove_queued_prompt")
    assert removals == 2, f"unexpected pre-delivery queue removals: {removals}"
    assert "batched = [params]" not in processor


def test_invalid_promote_action_fails_closed() -> None:
    websocket = _function_source(BACKEND / "main.py", "websocket_chat")
    promote = websocket.split('elif msg_type == "promote_queued":', 1)[1]
    promote = promote.split('elif msg_type == "cancel_queued":', 1)[0]
    assert 'action = "interrupt"' not in promote
    assert 'action not in ("interrupt", "steer")' in promote
    assert "continue" in promote.split("coordinator.promote_queued", 1)[0]


if __name__ == "__main__":
    test_queue_record_survives_until_user_message_persist()
    test_invalid_promote_action_fails_closed()
    print("PASS prompt delivery invariants")
