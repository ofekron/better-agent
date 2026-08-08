import re
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "ws_chat.py").read_text(encoding="utf-8")


def test_subscribe_replay_runs_blocking_work_off_the_event_loop():
    # tick_running_state is fired off-loop AND non-blocking via create_task(to_thread(...)).
    assert re.search(
        r"asyncio\.create_task\(\s+asyncio\.to_thread\(\s+coordinator\.turn_manager\.tick_running_state,",
        SOURCE,
    )
    # The replay delta is built in a worker thread, not inline on the loop.
    assert re.search(
        r"delta = await asyncio\.to_thread\(\s+_build_messages_replay_delta,",
        SOURCE,
    )
    # The replay builder is imported from the session_detail_api projection module.
    assert "_build_messages_replay_delta," in SOURCE
    # The previously-inline blocking call has moved out of this module entirely.
    assert "get_messages_since" not in SOURCE
    assert "tick_running_state(sub_sid)" not in SOURCE
