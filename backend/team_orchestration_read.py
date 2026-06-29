from __future__ import annotations

from typing import Any

from i18n import t
from session_manager import manager as session_manager
import perf
import session_store


def list_workers_for_cwd(cwd: str) -> dict[str, Any]:
    from stores import worker_store as worker_store

    with perf.timed("extension.team_orchestration.workers.registry"):
        raw = worker_store._read()
        workers = sorted(
            raw.get("workers", []),
            key=lambda worker: worker.get("last_active", ""),
            reverse=True,
        )
    worker_sids = [str(worker.get("agent_session_id") or "") for worker in workers]
    fields = ("agent_session_id", "cwd", "name", "orchestration_mode")
    with perf.timed("extension.team_orchestration.workers.summary_fields"):
        fields_by_sid = session_store.summary_fields_many(worker_sids, fields)
    forks = raw.get("forks", {}) or {}
    out: list[dict[str, Any]] = []
    for worker in workers:
        bc_sid = worker.get("agent_session_id")
        bc = fields_by_sid.get(bc_sid) if bc_sid else None
        if not bc:
            continue
        mode = worker.get("orchestration_mode") or bc.get("orchestration_mode") or "native"
        worker_cwd = worker.get("cwd") or bc.get("cwd") or cwd
        live_parent_sid = bc.get("agent_session_id")
        sid_rotated = bool(
            live_parent_sid
            and worker.get("agent_sid")
            and live_parent_sid != worker.get("agent_sid")
        )
        any_pair_stale = False
        pair_records: list[dict[str, Any]] = []
        if not sid_rotated and live_parent_sid:
            with perf.timed("extension.team_orchestration.workers.fork_scan"):
                for _caller_sid, by_worker in forks.items():
                    rec = by_worker.get(bc_sid)
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("parent_agent_sid") != live_parent_sid:
                        any_pair_stale = True
                        break
                    pair_records.append(rec)
        if not any_pair_stale and pair_records and live_parent_sid:
            from orchs.jsonl_helpers import compute_jsonl_path, count_jsonl_lines

            with perf.timed("extension.team_orchestration.workers.divergence_lines"):
                live_parent_lines = 0
                path = compute_jsonl_path(worker_cwd, live_parent_sid)
                if path:
                    try:
                        live_parent_lines = count_jsonl_lines(path)
                    except Exception:
                        live_parent_lines = 0
                for rec in pair_records:
                    if int(rec.get("parent_line_count_at_fork", 0)) < live_parent_lines:
                        any_pair_stale = True
                        break
        diverged = sid_rotated or any_pair_stale
        out.append({
            "agent_session_id": bc_sid,
            "name": worker.get("name") or bc.get("name") or t("session.untitled_worker"),
            "display_name": bc.get("name") or t("session.untitled_worker"),
            "role_key": worker.get("role_key"),
            "cwd": worker_cwd,
            "registry_cwd": worker_cwd,
            "orchestration_mode": mode,
            "agent_sid": worker.get("agent_sid"),
            "live_parent_agent_sid": live_parent_sid,
            "initialized": bool(live_parent_sid),
            "diverged": diverged,
            "created_at": worker.get("created_at"),
            "last_active": worker.get("last_active"),
            "delegation_count": worker.get("delegation_count", 0),
            "token_usage": worker.get("token_usage"),
            "tags": worker_store.normalize_tags(worker.get("tags")),
        })
    return {
        "workers": out,
        "pools": _worker_pool_projection(out, raw.get("pool_queues") or {}),
        "teams": _worker_team_projection(out),
    }


def _worker_pool_projection(workers: list[dict[str, Any]], pool_queues: dict) -> list[dict[str, Any]]:
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for worker in workers:
        for tag in worker.get("tags") or []:
            by_tag.setdefault(str(tag), []).append(worker)
    pools = []
    for tag, tagged_workers in sorted(by_tag.items()):
        queue = pool_queues.get(tag)
        pools.append({
            "tag": tag,
            "workers": tagged_workers,
            "queued_count": len(queue) if isinstance(queue, list) else 0,
        })
    return pools


def _worker_team_projection(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import team_store

    workers_by_id = {worker.get("agent_session_id"): worker for worker in workers}
    teams = []
    for team in team_store.list_all():
        members = team_store.ordered_members(team)
        bound_ids = {
            member.get("agent_session_id")
            for member in members
            if member.get("type") == "worker" and member.get("agent_session_id")
        }
        worker_rows = []
        for member in members:
            if member.get("type") != "worker":
                continue
            sid = member.get("agent_session_id")
            worker = workers_by_id.get(sid)
            if worker:
                worker_rows.append({**worker, "team_binding": "bound", "team_role": member.get("role")})
        for worker in workers:
            if worker.get("agent_session_id") in bound_ids:
                continue
            worker_rows.append({**worker, "team_binding": "available", "team_role": ""})
        teams.append({
            "id": team.get("id"),
            "name": team.get("definition_ref") or team.get("profile") or team.get("id"),
            "root_session_id": team.get("root_session_id"),
            "workers": worker_rows,
        })
    return teams
