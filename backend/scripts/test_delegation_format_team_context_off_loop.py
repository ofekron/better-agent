from __future__ import annotations

from pathlib import Path


def test_delegation_format_team_context_is_off_loop() -> None:
    source = (
        Path(__file__).parents[1] / "orchs" / "manager" / "_delegation.py"
    ).read_text(encoding="utf-8")
    assert "await asyncio.to_thread(\n            manager_bootstrap.format_team_context," in source
    assert "worker_prompt = \"\\n\\n\".join([\n            manager_bootstrap.format_team_context(" not in source


if __name__ == "__main__":
    test_delegation_format_team_context_is_off_loop()
    print("PASS delegation format_team_context off loop")
