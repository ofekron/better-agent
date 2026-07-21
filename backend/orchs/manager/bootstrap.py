"""Manager-mode prompt assembly.

Three pieces:

  - `BOOTSTRAP_PROMPT` — the long system-bootstrap block injected on
    the first manager turn of a session. Tells the manager to delegate,
    sample worker jsonls, classify outcomes, iterate, and reply.
  - `format_known_workers(workers)` — renders the global worker
    registry as a `<known_workers>` block included on every turn.
  - `build_wrapped_prompt(cwd, user_prompt, is_first_turn, known_workers)`
    — composes the final prompt sent to the manager Claude session.
"""

from html import escape

from prompt_templates import render_prompt
import team_store
from stores import worker_store


BOOTSTRAP_PROMPT = render_prompt("manager/bootstrap.md")

def format_known_workers(workers: list[dict]) -> str:
    """Render the global worker registry. Positional columns under a
    single header line keep the per-worker cost ~half of the previous
    `key: value | key: value` form while preserving every field the
    manager bootstrap references (`agent_session_id`, `mode`,
    `description`). `last_active` is dropped — the bootstrap selects
    workers by description match, never by recency.
    """
    if not workers:
        return (
            "<known_workers>\n"
            "No workers yet. Call create_worker to request a fresh worker.\n"
            "</known_workers>"
        )
    lines = [
        "<known_workers>",
        "Columns: agent_session_id | registry_cwd | mode | node | turns | description "
        "(pass agent_session_id and registry_cwd to `ask` run_mode=\"direct\" to resume)",
    ]
    for w in workers:
        registry_cwd = w.get("registry_cwd") or w.get("cwd") or ""
        lines.append(
            f"{w.get('agent_session_id')} "
            f"{registry_cwd} "
            f"{w.get('orchestration_mode', '?')} "
            f"{w.get('node_id', 'primary')} "
            f"{w.get('delegation_count', 0)} "
            f"\"{w.get('description', '')}\""
        )
    lines.append("</known_workers>")
    return "\n".join(lines)


def format_team_context(
    *,
    cwd: str,
    self_session_id: str,
    self_role: str,
    self_description: str,
    workers: list[dict] | None = None,
    team_members: list[dict] | None = None,
    manager_session_id: str | None = None,
    manager_description: str = "manager",
) -> str:
    members = list(team_members) if team_members is not None else []
    if not members:
        runtime_team = team_store.find_for_session(self_session_id)
        if runtime_team:
            members = [
                {
                    "id": member.get("id") or "",
                    "session_id": member.get("agent_session_id") or "",
                    "role": member.get("role") or member.get("type") or "",
                    "type": member.get("type") or "",
                    "description": member.get("description") or "",
                }
                for member in team_store.ordered_members(runtime_team)
            ]
    if not members:
        team_workers = (
            list(workers)
            if workers is not None
            else worker_store.list_worker_projection(cwd, limit=20)
        )
        if manager_session_id:
            members.append({
                "session_id": manager_session_id,
                "role": "manager",
                "description": manager_description,
            })
        for worker in team_workers:
            members.append({
                "session_id": worker.get("agent_session_id") or "",
                "role": "worker",
                "description": worker.get("description") or "",
            })
    seen = set()
    member_lines = []
    for member in members:
        sid = member.get("session_id") or ""
        if not sid or sid in seen:
            continue
        seen.add(sid)
        id_attr = ""
        if member.get("id"):
            id_attr = f'id="{escape(member.get("id") or "", quote=True)}" '
        type_attr = ""
        if member.get("type"):
            type_attr = f'type="{escape(member.get("type") or "", quote=True)}" '
        member_lines.append(
            '<member '
            f"{id_attr}"
            f'session_id="{escape(sid, quote=True)}" '
            f'role="{escape(member.get("role") or "", quote=True)}" '
            f"{type_attr}"
            f'description="{escape(member.get("description") or "", quote=True)}" '
            "/>"
        )
    return "\n".join([
        "<self>",
        f"<session_id>{escape(self_session_id)}</session_id>",
        f"<role>{escape(self_role)}</role>",
        f"<description>{escape(self_description)}</description>",
        "</self>",
        "<team>",
        *member_lines,
        "</team>",
        "<messaging>",
        "Use mssg(target_session_id, message) to send a message to any team member by that member's session_id.",
        "Use mssg for one-way coordination.",
        "Use inbox(recipient_session_id, message) to return final results requested by async ask or delegate_task.",
        "Call inbox() to read your own pending results.",
        "</messaging>",
    ])


def build_wrapped_prompt(
    cwd: str,
    user_prompt: str,
    is_first_turn: bool,
    known_workers: list[dict] | None = None,
    self_session_id: str | None = None,
    self_role: str = "manager",
    self_description: str = "manager",
    manager_session_id: str | None = None,
    manager_description: str = "manager",
) -> str:
    workers = (
        list(known_workers)
        if known_workers is not None
        else worker_store.list_worker_projection(cwd, limit=20)
    )
    known_block = format_known_workers(workers)
    team_block = ""
    if self_session_id:
        team_block = format_team_context(
            cwd=cwd,
            self_session_id=self_session_id,
            self_role=self_role,
            self_description=self_description,
            workers=workers,
            manager_session_id=manager_session_id or self_session_id,
            manager_description=manager_description,
        )
    user_block = f"<user_prompt>\n{user_prompt}\n</user_prompt>"
    context_blocks = "\n\n".join(
        block for block in (known_block, team_block) if block
    )
    if is_first_turn:
        return f"{BOOTSTRAP_PROMPT}\n\n{context_blocks}\n\n{user_block}"
    return f"{context_blocks}\n\n{user_block}"
