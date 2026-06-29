from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Optional

from prompt_templates import render_prompt
from session_manager import manager as session_manager
import team_store
from stores import worker_store


SOURCE = "team_message"
ASK_SOURCE = "team_ask"
MSSG_RESPONSE_MODE = "mssg"


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


def _target_team_context(target_session_id: Optional[str]) -> str:
    if not target_session_id:
        return ""
    target = session_manager.get_lite(target_session_id)
    if not target:
        return ""
    cwd = str(target.get("cwd") or "")
    workers = worker_store.list_worker_projection(cwd, limit=20) if cwd else []
    self_role = "manager"
    self_description = str(target.get("name") or "manager")
    runtime_team = team_store.find_for_session(target_session_id)
    if runtime_team:
        member = team_store.member_for_session(runtime_team, target_session_id)
        if member:
            self_role = str(member.get("role") or member.get("type") or self_role)
            self_description = str(member.get("description") or self_description)
    for worker in workers:
        if worker.get("agent_session_id") == target_session_id:
            self_role = "worker"
            self_description = str(worker.get("description") or self_description)
            break
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
) -> str:
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
        )
    blocks = [
        format_team_message_prompt(
            str(item.get("message") or ""),
            dict(item.get("metadata") or {}),
        )
        for item in items
    ]
    team_context = _target_team_context(target_session_id)
    batch = "<team_messages>\n" + "\n\n".join(blocks) + "\n</team_messages>"
    return f"{team_context}\n\n{batch}" if team_context else batch


def queue_payload(
    *,
    queue_item_id: str,
    sender_session_id: str,
    message: str,
    metadata: dict,
    lifecycle_msg_id: str,
    target_session_id: Optional[str] = None,
    source: str = SOURCE,
) -> dict:
    return {
        "id": queue_item_id,
        "content": message,
        "cli_prompt": format_team_message_prompt(
            message,
            metadata,
            target_session_id=target_session_id,
        ),
        "source": source,
        "sender_session_id": sender_session_id,
        "created_at": datetime.now().isoformat(),
        "lifecycle_msg_id": lifecycle_msg_id,
    }
