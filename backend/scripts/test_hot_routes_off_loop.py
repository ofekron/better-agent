from pathlib import Path


def main() -> int:
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert "return await asyncio.to_thread(\n        requirement_context.search_requirements," in source
    assert "sess = await asyncio.to_thread(\n        session_manager.mark_seen," in source
    assert "session = await asyncio.to_thread(session_manager.get_lite, session_id)" in source
    assert (
        "_sess = await asyncio.to_thread(\n"
        "                        session_manager.get_lite,\n"
        "                        app_session_id,\n"
        "                    )"
    ) in source
    assert (
        "_alter_session = await asyncio.to_thread(\n"
        "                        session_manager.get_lite,\n"
        "                        app_session_id,\n"
        "                    )"
    ) in source
    assert "await asyncio.to_thread(\n        session_manager.set_draft," in source
    assert "REMOTE_SESSION_MERGE_TIMEOUT_SECONDS = 0.75" in source
    assert "timeout=REMOTE_SESSION_MERGE_TIMEOUT_SECONDS," in source
    assert "timeout=REMOTE_SESSION_MERGE_TIMEOUT_SECONDS + 0.05," in source
    assert "return_exceptions=True" in source
    print("PASS: hot request routes keep blocking session/requirement work off the event loop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
