from pathlib import Path
import re


def main() -> int:
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert "_REQUIREMENTS_QUERY_EXECUTOR" not in source
    assert "_run_requirements_query" not in source
    assert "run_requirements_query(\n        \"requirements.processed\"," in source
    assert "executor=REQUIREMENTS_PROCESSOR_EXECUTOR" in source
    assert "run_requirements_query(\n        \"requirements.search\"," in source
    assert "executor=REQUIREMENTS_SEARCH_EXECUTOR" in source
    assert "asyncio.to_thread(\n        requirement_context.get_processed_requirements," not in source
    assert "asyncio.to_thread(\n        requirement_context.search_requirements," not in source
    assert "sess = await asyncio.to_thread(\n        session_manager.mark_seen," in source
    assert "session = await asyncio.to_thread(session_manager.get_lite, session_id)" in source
    assert re.search(r"_offline_session = await asyncio\.to_thread\(\s+session_manager\.get_lite,\s+app_session_id,\s+\)", source)
    assert re.search(r"_alter_session = await asyncio\.to_thread\(\s+session_manager\.get_lite,\s+app_session_id,\s+\)", source)
    assert "await asyncio.to_thread(\n        session_manager.set_draft," in source
    assert "REMOTE_SESSION_MERGE_TIMEOUT_SECONDS = 0.75" in source
    assert "timeout=REMOTE_SESSION_MERGE_TIMEOUT_SECONDS," in source
    assert "timeout=REMOTE_SESSION_MERGE_TIMEOUT_SECONDS + 0.05," in source
    assert "return_exceptions=True" in source
    print("PASS: hot request routes keep blocking session/requirement work off the event loop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
