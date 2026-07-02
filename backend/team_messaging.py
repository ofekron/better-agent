from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Optional

from prompt_templates import render_prompt
from session_manager import manager as session_manager
import team_store
from stores import worker_store


SOURCE = "mssg"
ASK_SOURCE = "team_ask"
UPDATE_SOURCE = "update"
DELEGATE_TASK_SOURCE = "delegate_task"
MESSAGE_SOURCES = (SOURCE, ASK_SOURCE, UPDATE_SOURCE, DELEGATE_TASK_SOURCE)
MSSG_RESPONSE_MODE = "mssg"
COLLAPSE_POLICY_TAKE_LATEST = "take_latest"
COLLAPSE_POLICIES = (COLLAPSE_POLICY_TAKE_LATEST,)


def validate_message_route(
    *,
    sender_session_id: str,
    target_session_id: str,
) -> tuple[dict, dict]:
    sender = session_manager.get_lite(sender_session_id)
    target = session_manager.get_lite(target_session_id)
    if not sender:
        raise ValueError("sender_session_id does not exist")
    if not target:
        raise ValueError("target_session_id does not exist")
    return sender, target


def build_message_metadata(
    *,
    sender_session_id: str,
    target_session_id: Optional[str] = None,
) -> dict:
    cwd = str(session_manager.get_field(sender_session_id, "cwd") or "")
    metadata = {
        "sender_session_id": sender_session_id,
    }
    target_cwd = (
        str(session_manager.get_field(target_session_id, "cwd") or "")
        if target_session_id else ""
    )
    if cwd and target_cwd and cwd != target_cwd:
        metadata["sender_cwd"] = cwd
    return metadata


def source_for_message_route(sender: dict, target: dict) -> str:
    if (
        sender.get("id") == target.get("id")
        and target.get("source") == "extension"
        and target.get("name") == "Assistant"
    ):
        return UPDATE_SOURCE
    return SOURCE


def _target_team_context(target_session_id: Optional[str]) -> str:
    if not target_session_id:
        return ""
    target = session_manager.get_lite(target_session_id)
    if not target:
        return ""
    cwd = str(target.get("cwd") or "")
    workers = worker_store.list_worker_projection(cwd, limit=20) if cwd else []
    self_worker = next(
        (w for w in workers if w.get("agent_session_id") == target_session_id),
        None,
    )
    runtime_team = team_store.find_for_session(target_session_id)
    member = (
        team_store.member_for_session(runtime_team, target_session_id)
        if runtime_team
        else None
    )
    is_manager = str(target.get("orchestration_mode") or "") in ("manager", "team")
    # Target has no real team membership: a synthesized context would invent
    # a team the target never had, so send none.
    if not member and not self_worker and not is_manager:
        return ""
    if member:
        self_role = str(member.get("role") or member.get("type") or "manager")
        self_description = str(
            member.get("description") or target.get("name") or self_role
        )
    elif self_worker:
        self_role = "worker"
        self_description = str(
            self_worker.get("description") or target.get("name") or "worker"
        )
    else:
        self_role = "manager"
        self_description = str(target.get("name") or "manager")
    from orchs.manager import bootstrap as manager_bootstrap
    return manager_bootstrap.format_team_context(
        cwd=cwd,
        self_session_id=target_session_id,
        self_role=self_role,
        self_description=self_description,
        workers=workers,
        manager_session_id=target_session_id if self_role == "manager" else None,
        manager_description=self_description,
    )


def format_team_message_prompt(
    message: str,
    metadata: dict,
    *,
    target_session_id: Optional[str] = None,
    wrapper_tag: str = "mssg",
) -> str:
    if wrapper_tag not in ("mssg", "delegated-task"):
        raise ValueError("wrapper_tag must be 'mssg' or 'delegated-task'")
    attrs = {
        "sender_session_id": metadata.get("sender_session_id") or "",
        "expects_response": str(bool(metadata.get("expects_response"))).lower()
        if "expects_response" in metadata
        else "",
    }
    rendered_attrs = " ".join(
        f'{key}="{escape(str(value), quote=True)}"'
        for key, value in attrs.items()
        if value
    )
    cross_cwd_note = ""
    if metadata.get("sender_cwd"):
        cross_cwd_note = render_prompt(
            "team/cross_cwd_note.md",
            {"sender_cwd": escape(str(metadata["sender_cwd"]))},
        )
    response_contract = ""
    if metadata.get("response_mode") == MSSG_RESPONSE_MODE and metadata.get("sender_session_id"):
        response_contract = (
            "\n\n<response_contract>\n"
            "When the task is complete, call "
            f'mssg(target_session_id="{escape(str(metadata["sender_session_id"]), quote=True)}", '
            "message=<result>) to send the result back to the sender.\n"
            "Use mssg for the final result even though this incoming message is asynchronous.\n"
            "</response_contract>"
        )
    team_context = _target_team_context(target_session_id)
    prompt = render_prompt(
        "team/message.md",
        {
            "wrapper_tag": wrapper_tag,
            "rendered_attrs": rendered_attrs,
            "cross_cwd_note": cross_cwd_note,
            "message": f"{message}{response_contract}",
        },
    )
    return f"{team_context}\n\n{prompt}" if team_context else prompt


def format_team_message_batch(
    items: list[dict],
    *,
    target_session_id: Optional[str] = None,
) -> str:
    if len(items) == 1:
        return format_team_message_prompt(
            str(items[0].get("message") or ""),
            dict(items[0].get("metadata") or {}),
            target_session_id=target_session_id,
            wrapper_tag=str(items[0].get("wrapper_tag") or "mssg"),
        )
    blocks = [
        format_team_message_prompt(
            str(item.get("message") or ""),
            dict(item.get("metadata") or {}),
            wrapper_tag=str(item.get("wrapper_tag") or "mssg"),
        )
        for item in items
    ]
    team_context = _target_team_context(target_session_id)
    batch = "<mssgs>\n" + "\n\n".join(blocks) + "\n</mssgs>"
    return f"{team_context}\n\n{batch}" if team_context else batch


def team_message_from_queue_payload(
    payload: dict,
    *,
    target_session_id: str,
) -> Optional[dict]:
    if payload.get("source") not in MESSAGE_SOURCES:
        return None
    sender_session_id = str(payload.get("sender_session_id") or "")
    metadata = (
        dict(payload.get("metadata") or {})
        if isinstance(payload.get("metadata"), dict)
        else build_message_metadata(
            sender_session_id=sender_session_id,
            target_session_id=target_session_id,
        )
    )
    source = str(payload.get("source") or "")
    return {
        "message": payload.get("content", ""),
        "metadata": metadata,
        "wrapper_tag": "delegated-task" if source == DELEGATE_TASK_SOURCE else "mssg",
    }


def queue_payload(
    *,
    queue_item_id: str,
    sender_session_id: str,
    message: str,
    metadata: dict,
    lifecycle_msg_id: str,
    target_session_id: Optional[str] = None,
    source: str = SOURCE,
    collapse_key: str = "",
    collapse_policy: str = "",
) -> dict:
    wrapper_tag = "delegated-task" if source == DELEGATE_TASK_SOURCE else "mssg"
    payload = {
        "id": queue_item_id,
        "content": message,
        "wrapper_tag": wrapper_tag,
        "metadata": dict(metadata),
        "cli_prompt": format_team_message_prompt(
            message,
            metadata,
            target_session_id=target_session_id,
            wrapper_tag=wrapper_tag,
        ),
        "source": source,
        "sender_session_id": sender_session_id,
        "created_at": datetime.now().isoformat(),
        "lifecycle_msg_id": lifecycle_msg_id,
    }
    if collapse_key:
        payload["collapse_key"] = collapse_key
        payload["collapse_policy"] = collapse_policy or COLLAPSE_POLICY_TAKE_LATEST
    return payload
