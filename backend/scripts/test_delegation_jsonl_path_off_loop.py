from __future__ import annotations

from pathlib import Path


def test_delegation_jsonl_path_resolution_is_off_loop() -> None:
    source = (Path(__file__).parents[1] / "orchs" / "manager" / "_delegation.py").read_text(
        encoding="utf-8"
    )
    assert "async def _compute_jsonl_read_path_off_loop(" in source
    assert "return await asyncio.to_thread(compute_jsonl_read_path, cwd, agent_sid, session)" in source
    assert "compute_jsonl_read_path(" not in source.replace(
        "asyncio.to_thread(compute_jsonl_read_path, cwd, agent_sid, session)",
        "",
    )


if __name__ == "__main__":
    test_delegation_jsonl_path_resolution_is_off_loop()
    print("PASS delegation jsonl path off loop")
