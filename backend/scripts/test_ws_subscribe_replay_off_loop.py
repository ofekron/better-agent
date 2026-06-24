from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    source = (root / "main.py").read_text(encoding="utf-8")
    assert "asyncio.create_task(\n                            asyncio.to_thread(\n                                coordinator.turn_manager.tick_running_state" in source
    assert "delta = await asyncio.to_thread(\n                            session_manager.get_messages_since" in source
    assert "coordinator.turn_manager.tick_running_state(sub_sid)\n                        delta = session_manager.get_messages_since" not in source
    print("PASS: websocket subscribe replay avoids blocking the event loop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
