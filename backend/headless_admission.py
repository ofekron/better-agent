from __future__ import annotations

from typing import Any

from headless_request_contract import (
    HeadlessAdmissionError,
    HeadlessAuthority,
    HeadlessRequest,
    admit_headless_request,
)


_SESSION_FIELDS = (
    "id",
    "provider_id",
    "model",
    "reasoning_effort",
    "runner",
    "node_id",
    "cwd",
    "agent_session_id",
)
_PERMISSION_SCOPES = {
    "internal_generation",
    "spawn_runs",
    "task_assessment",
}


def _session_snapshot(session_id: str) -> dict | None:
    from session_manager import manager

    return manager.get_fields(session_id, _SESSION_FIELDS)


def _provider_record(provider_id: str) -> dict | None:
    import config_store

    return config_store.get_provider(provider_id)


def _session_executor(session: dict, provider_id: str, runner: str) -> Any:
    node_id = str(session.get("node_id") or "primary")
    try:
        from topology import local_node_id

        local_id = local_node_id()
    except Exception:
        local_id = "primary"
    if node_id != local_id:
        import extension_store
        import provider_remote

        not_ready = extension_store.runtime_not_ready_message(
            extension_store.extension_id_for_role("machine-nodes")
        )
        if not_ready is not None:
            raise RuntimeError(not_ready)
        return provider_remote.get_proxy(node_id)
    from provider import get_provider

    executor = get_provider(provider_id, runner)
    if getattr(executor, "suspended", False):
        raise RuntimeError(f"provider {provider_id} is suspended")
    if getattr(executor, "defunct", False):
        raise RuntimeError(f"provider {provider_id} is defunct")
    return executor


def _provider_capabilities(provider_id: str, runner: str) -> Any:
    from provider import get_provider

    return get_provider(provider_id, runner)


def _required_string(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"headless session {label} is unavailable")
    return value.strip()


async def run_session_headless(
    session_id: str,
    *,
    prompt: str,
    fork: bool,
    resume: bool,
    no_tools: bool,
    timeout: float | None,
    permission_scope: str,
    expected_provider_id: str = "",
    expected_model: str = "",
) -> dict:
    if permission_scope not in _PERMISSION_SCOPES:
        raise ValueError("headless permission scope is invalid")
    if type(fork) is not bool or type(resume) is not bool or type(no_tools) is not bool:
        raise ValueError("headless execution flags must be booleans")
    if fork and resume:
        raise ValueError("headless fork and resume are mutually exclusive")
    session_id = _required_string(session_id, "id")
    session = _session_snapshot(session_id)
    if type(session) is not dict:
        raise ValueError("headless session is unavailable")
    provider_id = _required_string(session.get("provider_id"), "provider")
    model = _required_string(session.get("model"), "model")
    if expected_provider_id and expected_provider_id != provider_id:
        raise ValueError("headless provider selector is stale")
    if expected_model and expected_model != model:
        raise ValueError("headless model selector is stale")
    record = _provider_record(provider_id)
    if type(record) is not dict:
        raise ValueError("headless provider authority is unavailable")
    from runtime_profile import resolve_runner

    runner = resolve_runner(record, session.get("runner"))
    capabilities = _provider_capabilities(provider_id, runner)
    resume_sid = None
    if fork or resume:
        resume_sid = _required_string(
            session.get("agent_session_id"),
            "provider session",
        )
    request = HeadlessRequest.from_dict({
        "schema": 1,
        "prompt": prompt,
        "owner": {"kind": "session", "id": session_id},
        "fork": fork,
        "no_tools": no_tools,
        "timeout": timeout,
    })
    try:
        admitted = admit_headless_request(
            request,
            HeadlessAuthority.create(
                owner_kind="session",
                owner_id=session_id,
                provider={
                    key: record[key]
                    for key in (
                        "id",
                        "kind",
                        "generation",
                        "execution_revision",
                    )
                },
                model=model,
                reasoning_effort=str(session.get("reasoning_effort") or ""),
                runner=runner,
                permission_scope=permission_scope,
                routing={"node_id": str(session.get("node_id") or "primary")},
                cwd=_required_string(session.get("cwd"), "cwd"),
                resume_sid=resume_sid,
                supports_fork=bool(capabilities.supports_fork),
                supports_no_tools=bool(capabilities.supports_headless_no_tools),
            ),
        )
    except HeadlessAdmissionError as exc:
        raise ValueError(str(exc)) from exc
    result = await _session_executor(session, provider_id, runner).run_admitted_headless(
        admitted
    )
    if type(result) is not dict:
        raise RuntimeError("headless provider result is invalid")
    return result
