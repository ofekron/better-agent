from __future__ import annotations

import collections
import json
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from i18n import t
from session_manager import manager as session_manager
import perf
import session_store


_METRIC = "extension.team_orchestration.workers"


@dataclass(frozen=True)
class _ProjectionEntry:
    token: tuple[Any, ...]
    payload: bytes
    result: dict[str, Any]
    native_paths: tuple[str, ...]
    native_token: tuple[tuple[str, int], ...]
    activity_token: tuple[str, int]


class _WorkersProjectionOwner:
    _MAX_ENTRIES = 32
    _MAX_BYTES = 16 * 1024 * 1024

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._entries: collections.OrderedDict[str, _ProjectionEntry] = collections.OrderedDict()
        self._building: set[str] = set()
        self._bytes = 0
        self._revision = 0
        self._cold_builds = 0

    def payload(self, cwd: str) -> bytes:
        request_started = time.perf_counter()
        key = "global"
        while True:
            base_token = _dependency_revision()
            with self._condition:
                entry = self._entries.get(key)
                if (
                    entry is not None
                    and base_token == entry.token
                    and _native_revision_token(entry.native_paths) == entry.native_token
                    and _activity_revision() == entry.activity_token
                ):
                    self._entries.move_to_end(key)
                    perf.record(f"{_METRIC}.warm", (time.perf_counter() - request_started) * 1000)
                    perf.record_count(f"{_METRIC}.warm_hits")
                    return entry.payload
                if key in self._building:
                    self._condition.wait()
                    continue
                self._building.add(key)
            try:
                while True:
                    base_token = _dependency_revision()
                    result, native_paths, observed_native_token, native_stable, activity_token = (
                        _build_workers_projection(cwd)
                    )
                    with perf.timed(f"{_METRIC}.json"):
                        payload = json.dumps(
                            result,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    perf.record_count(f"{_METRIC}.bytes", len(payload))
                    final_token = _dependency_revision()
                    final_native_token = _native_revision_token(native_paths)
                    if (
                        final_token != base_token
                        or _activity_revision() != activity_token
                        or not native_stable
                        or final_native_token != observed_native_token
                    ):
                        continue
                    with self._condition:
                        if (
                            _dependency_revision() != final_token
                            or _activity_revision() != activity_token
                            or _native_revision_token(native_paths) != final_native_token
                        ):
                            continue
                        prior = self._entries.pop(key, None)
                        if prior is not None:
                            self._bytes -= len(prior.payload)
                        self._entries[key] = _ProjectionEntry(
                            token=final_token,
                            payload=payload,
                            result=result,
                            native_paths=native_paths,
                            native_token=final_native_token,
                            activity_token=activity_token,
                        )
                        self._bytes += len(payload)
                        while len(self._entries) > self._MAX_ENTRIES or self._bytes > self._MAX_BYTES:
                            _, evicted = self._entries.popitem(last=False)
                            self._bytes -= len(evicted.payload)
                        self._revision += 1
                        self._cold_builds += 1
                        perf.record(f"{_METRIC}.cold", (time.perf_counter() - request_started) * 1000)
                        perf.record_count(f"{_METRIC}.cold_builds")
                        return payload
            finally:
                with self._condition:
                    self._building.discard(key)
                    self._condition.notify_all()

    def apply_activity(self, commit: Any) -> None:
        started = time.perf_counter()
        with self._condition:
            for key, entry in tuple(self._entries.items()):
                commit_token = (commit.authority_epoch, commit.seq)
                if commit.authority_epoch == entry.activity_token[0] and commit.seq <= entry.activity_token[1]:
                    perf.record_count(f"{_METRIC}.activity_duplicates")
                    continue
                if (
                    commit.authority_epoch != entry.activity_token[0]
                    or commit.seq != entry.activity_token[1] + 1
                ):
                    self._entries.pop(key, None)
                    self._bytes -= len(entry.payload)
                    perf.record_count(f"{_METRIC}.activity_invalidations")
                    continue
                result = deepcopy(entry.result)
                changed = False
                for collection in _worker_collections(result):
                    for worker in collection:
                        if worker.get("agent_session_id") != commit.worker.get("agent_session_id"):
                            continue
                        worker.update(commit.worker)
                        changed = True
                    collection.sort(key=lambda worker: worker.get("last_active") or "", reverse=True)
                if not changed:
                    self._entries.pop(key, None)
                    self._bytes -= len(entry.payload)
                    perf.record_count(f"{_METRIC}.activity_invalidations")
                    continue
                result["authority_epoch"] = commit.authority_epoch
                result["revision"] = commit.seq
                payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._entries[key] = _ProjectionEntry(
                    token=entry.token,
                    payload=payload,
                    result=result,
                    native_paths=entry.native_paths,
                    native_token=entry.native_token,
                    activity_token=commit_token,
                )
                self._bytes += len(payload) - len(entry.payload)
                perf.record_count(f"{_METRIC}.activity_patches")
        perf.record(f"{_METRIC}.activity_patch", (time.perf_counter() - started) * 1000)

    def reset_for_tests(self) -> None:
        with self._condition:
            self._entries.clear()
            self._building.clear()
            self._bytes = 0
            self._revision = 0
            self._cold_builds = 0

    def stats_for_tests(self) -> tuple[int, int, int, int]:
        with self._condition:
            return self._revision, self._cold_builds, len(self._entries), self._bytes


_PROJECTION_OWNER = _WorkersProjectionOwner()


def _worker_collections(result: dict[str, Any]) -> list[list[dict[str, Any]]]:
    collections_out = [result.get("workers") or []]
    collections_out.extend(pool.get("workers") or [] for pool in result.get("pools") or [])
    collections_out.extend(team.get("workers") or [] for team in result.get("teams") or [])
    return collections_out


def apply_worker_activity(commit: Any) -> None:
    _PROJECTION_OWNER.apply_activity(commit)


def _dependency_revision() -> tuple[int, int, int]:
    from stores import worker_store
    import team_store

    session_store._ensure_summary_index(blocking=True)
    return (
        worker_store.revision(),
        session_store.summary_version(),
        team_store.revision(),
    )


def _native_revision_token(paths: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
    from orchs.jsonl_helpers import path_revision_token

    return path_revision_token(paths)


def _activity_revision() -> tuple[str, int]:
    from stores import worker_store

    return worker_store.activity_authority()


def workers_response_bytes(cwd: str, _request_shape: str = "") -> bytes:
    return _PROJECTION_OWNER.payload(str(cwd or ""))


def list_workers_for_cwd(cwd: str, request_shape: str = "") -> dict[str, Any]:
    return json.loads(workers_response_bytes(cwd, request_shape))


def _build_workers_projection(
    cwd: str,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[tuple[str, int], ...], bool, tuple[str, int]]:
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
    with perf.timed(f"{_METRIC}.session"):
        fields_by_sid = session_store.summary_fields_many(worker_sids, fields)
    forks = raw.get("forks", {}) or {}
    out: list[dict[str, Any]] = []
    native_paths: set[str] = set()
    native_before: dict[str, int] = {}
    with perf.timed(f"{_METRIC}.loops"):
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

                live_parent_lines = 0
                path = compute_jsonl_path(worker_cwd, live_parent_sid)
                if path:
                    from orchs.jsonl_helpers import path_revision

                    native_path = str(path)
                    native_paths.add(native_path)
                    native_before.setdefault(native_path, path_revision(path))
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
                "node_id": worker.get("node_id") or "primary",
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
    with perf.timed(f"{_METRIC}.team"):
        teams = _worker_team_projection(out)
    result = {
        "workers": out,
        "pools": _worker_pool_projection(out, raw.get("pool_queues") or {}),
        "teams": teams,
    }
    activity_token = worker_store.activity_authority()
    result["authority_epoch"], result["revision"] = activity_token
    ordered_native_paths = tuple(sorted(native_paths))
    observed_native_token = _native_revision_token(ordered_native_paths)
    native_stable = all(
        native_before[path] == revision
        for path, revision in observed_native_token
    )
    return result, ordered_native_paths, observed_native_token, native_stable, activity_token


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
