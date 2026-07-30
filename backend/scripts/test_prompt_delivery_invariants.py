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
    run_barrier = "await self.turn_manager.wait_for_clear_runs(app_session_id)"
    lifecycle_barrier = "await self.lifecycle_commands.wait_for_phase("
    begin_turn = "await self.lifecycle_commands.begin_turn("
    assert lifecycle_barrier in processor
    assert processor.index(run_barrier) < processor.index(lifecycle_barrier)
    assert processor.index(lifecycle_barrier) < processor.index(begin_turn)
    delivery_gate = processor[
        processor.index(run_barrier):
        processor.index("if is_review:")
    ]
    assert "await consume_queue_item()" not in delivery_gate
    assert "if item_id and await cleanup_cancelled_queue_item():" in delivery_gate
    initializer = _function_source(BACKEND / "orchestrator.py", "_init_turn_messages")
    assert initializer.index("append_user_msg(") < initializer.index(
        "remove_queued_prompt(app_session_id, queue_item_id)",
    )
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
