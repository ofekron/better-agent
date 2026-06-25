from __future__ import annotations

import extension_store


def record(*roles: str) -> dict:
    return {"manifest": {"id": "example.extension", "core_roles": list(roles)}}


def main() -> None:
    requirements = extension_store.extension_internal_llm_tasks(record("requirements"))
    assert requirements == ["requirement_analysis"]
    assert extension_store.extension_provisioned_internal_llm_tasks(
        record("requirements")
    ) == ["requirement_analysis"]

    team = extension_store.extension_internal_llm_tasks(record("team-orchestration"))
    assert team == ["delegation_task", "delegation_message", "delegation_ask"]

    keys = extension_store.all_internal_llm_task_keys()
    assert "requirement_analysis" in keys
    assert set(team).issubset(keys)
    assert set(requirements + team).issubset(extension_store.extension_internal_llm_task_keys())
    print("role internal LLM tasks: PASS")


if __name__ == "__main__":
    main()
