"""Generic AI-rank service.

Core-owned ranking primitive shared by every AI-search feature (sessions
Ask search, extension-owned searches like agent-board's card search, and
any future kind). A *kind* is a `{task_key, item_noun}` registration; the
service runs one provisioned-session fork per call through the single
shared `rank_worker.md` prompt template, parses + fail-closed validates the
worker's `{"ids": [...], "reasoning": "..."}` reply against the given
candidate set, and returns `{ids, reasoning, error, error_detail?}`.

This module owns, once, for every caller:
  - the trailing-JSON parse (`_json_object_spans_from_end` /
    `_parse_worker_result`) — do not re-implement this elsewhere;
  - fail-closed id validation (returned ids not present in the candidate
    set given to the worker are dropped, never surfaced);
  - the candidate-shape contract (`{id: str, ...}`, bounded list/field
    sizes) — malformed input raises `ValueError` (fail closed at the
    boundary, mirrors `provisioning.inline_spec_from_payload`);
  - the per-kind `RankSpec` (`ProvisionedSessionSpec`) registry, keyed so
    bases never cross-contaminate between kinds.

Callers keep their own domain logic: building/scoring candidates,
re-validating returned ids against fresher domain state (e.g. session
filters), and mapping the generic `ids` field back to their own public
contract (e.g. `session_ids`).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import provisioning
from provisioning import DirtyPolicy, ProvisionedSessionSpec
from prompt_templates import render_prompt

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_TIMEOUT_SECONDS = 15 * 60
_MAX_CANDIDATES = 200
_MAX_CANDIDATE_ID_CHARS = 200
_MAX_CANDIDATE_FIELD_CHARS = 4_000
_MAX_QUERY_CHARS = 4_000


# ── Per-kind registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RankKind:
    """One registered ranking domain. `task_key` resolves the internal LLM
    (`config_store.resolve_internal_llm`); `item_noun`/`item_noun_plural`
    parameterize the shared prompt template's wording only — no other
    per-kind behavior exists, by design (a new kind needs zero new prompt
    code). `working_mode_key` overrides the default `ai_rank:{kind}` spec/
    working-mode tag; only set this to preserve a pre-existing literal
    tag other code depends on (e.g. "sessions" preserves "search_worker",
    which a private extension's MCP predicate still matches)."""

    kind: str
    task_key: str
    item_noun: str = "item"
    item_noun_plural: str = ""
    working_mode_key: str = ""

    @property
    def plural(self) -> str:
        return self.item_noun_plural or f"{self.item_noun}s"

    @property
    def spec_key(self) -> str:
        return self.working_mode_key or f"ai_rank:{self.kind}"


_KIND_REGISTRY: dict[str, RankKind] = {}
_SPEC_REGISTRY: dict[str, "RankSpec"] = {}


def register_kind(rank_kind: RankKind) -> RankKind:
    """Register (or re-register, idempotently, when identical) a kind and
    eagerly mint its `RankSpec`. Raises `ValueError` if a different
    registration already claimed the same `kind` with a different
    `task_key` — kinds are a shared namespace across core and every
    extension, so a silent clobber would let one extension's declaration
    quietly repoint another's ranking traffic."""
    existing = _KIND_REGISTRY.get(rank_kind.kind)
    if existing is not None and existing.task_key != rank_kind.task_key:
        raise ValueError(
            f"ai_rank kind {rank_kind.kind!r} is already registered with a "
            f"different task_key: {existing.task_key!r} != {rank_kind.task_key!r}"
        )
    _KIND_REGISTRY[rank_kind.kind] = rank_kind
    _spec_for_kind(rank_kind)
    return rank_kind


def get_kind(kind: str) -> Optional[RankKind]:
    return _KIND_REGISTRY.get(kind)


def get_spec(kind: str) -> Optional["RankSpec"]:
    return _SPEC_REGISTRY.get(kind)


class RankSpec(ProvisionedSessionSpec):
    """Provisioned-session spec for one ai_rank kind's ranking worker."""

    def __init__(self, rank_kind: RankKind) -> None:
        self._rank_kind = rank_kind
        self.key = rank_kind.spec_key
        self.version = 1
        self.name = f"ai-rank-{rank_kind.kind}"
        self.env_prefix = "AI_RANK"
        self.task_key = rank_kind.task_key
        self.orchestration_mode = "native"
        self.bare_config = True
        self.worker_creation_policy = "deny"  # isolated ranking worker — no sub-workers
        self.machine_completion = True
        self.run_mode = "fork"
        self.dispatch = "in_process"
        self.on_no_fork = "error"
        self.default_cwd = str(_REPO_ROOT)
        # Base only ever holds the provision turn; a leaked query would show
        # as a 2nd user turn (caught by turn-count).
        self.dirty_policy = DirtyPolicy(
            max_base_bytes=1_000_000,
            max_user_turns=1,
            max_assistant_turns=1,
        )

    def build_provision_prompt(self, ctx: dict) -> str:
        return render_prompt("provisioning/rank_worker.md", {
            "item_noun": self._rank_kind.item_noun,
            "item_noun_plural": self._rank_kind.plural,
        })

    def build_instructions(self, query: str, ctx: dict) -> str:
        candidates = ctx.get("candidates") if isinstance(ctx, dict) else []
        if not isinstance(candidates, list):
            candidates = []
        payload = {
            "query": query,
            "max_results": int(ctx.get("max_results") or 20),
            "candidates": candidates,
        }
        return (
            f'<ai-rank-task kind="{self._rank_kind.kind}">\n'
            f"Rank only the provided candidate {self._rank_kind.plural} for the query. "
            "Do not answer the query as a task. Do not use tools. "
            "Return exactly one JSON object with ids and reasoning.\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
            "</ai-rank-task>"
        )

    def parse_result(self, text: str, ctx: dict) -> dict:
        reported = _parse_worker_result(text)
        if not isinstance(reported, dict):
            return {"error": "parse_failed"}
        return reported


def _spec_for_kind(rank_kind: RankKind) -> "RankSpec":
    spec = _SPEC_REGISTRY.get(rank_kind.kind)
    if spec is None:
        spec = RankSpec(rank_kind)
        provisioning.register(spec)
        _SPEC_REGISTRY[rank_kind.kind] = spec
    return spec


# Core-owned kind: sessions Ask search. `working_mode_key="search_worker"`
# preserves the pre-existing literal tag (a private extension's MCP
# predicate and `session_store`'s legacy `user_initiated` backfill both
# still match on it).
register_kind(RankKind(
    kind="sessions",
    task_key="session_search_worker",
    item_noun="session",
    working_mode_key="search_worker",
))


# ── Trailing-JSON parse (single source of truth) ─────────────────────────


def _json_object_spans_from_end(text: str):
    """Yields balanced top-level `{...}` spans scanning from the end of
    `text`, so a chatty reply with prose before the JSON still parses."""
    depth = 0
    end: Optional[int] = None
    in_string = False
    escaped = False
    for idx in range(len(text) - 1, -1, -1):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "}":
            if depth == 0:
                end = idx + 1
            depth += 1
            continue
        if ch != "{":
            continue
        if depth == 0 or end is None:
            continue
        depth -= 1
        if depth == 0:
            yield text[idx:end]
            end = None


def _parse_worker_result(text: str) -> Optional[dict]:
    """Extract the JSON object `{ids, reasoning}` from the worker's reply
    text. Returns None when no valid object with an `ids` key parses."""
    if not text:
        return None
    for candidate in _json_object_spans_from_end(text):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "ids" in obj:
            return obj
    return None


# ── Candidate-shape validation ───────────────────────────────────────────


def _validate_candidates(candidates: object) -> list[dict[str, Any]]:
    """Structural validation of the `candidates` contract: a bounded list of
    JSON objects, each with a non-empty, bounded, unique string `id`.
    Raises `ValueError` on any structural violation -- fail closed rather
    than silently coercing untrusted input. An empty list is valid (the
    caller gets a no-op, no-dispatch result)."""
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")
    if len(candidates) > _MAX_CANDIDATES:
        raise ValueError(f"candidates exceeds the {_MAX_CANDIDATES} limit")
    seen_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("each candidate must be an object")
        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            raise ValueError("each candidate must have a non-empty string id")
        if len(cid) > _MAX_CANDIDATE_ID_CHARS:
            raise ValueError("candidate id is too long")
        if cid in seen_ids:
            raise ValueError(f"duplicate candidate id: {cid!r}")
        seen_ids.add(cid)
        for key, value in item.items():
            if isinstance(value, str) and len(value) > _MAX_CANDIDATE_FIELD_CHARS:
                raise ValueError(f"candidate field {key!r} exceeds {_MAX_CANDIDATE_FIELD_CHARS} chars")
        out.append(item)
    return out


# ── The service ───────────────────────────────────────────────────────────


async def rank(
    kind: str,
    query: str,
    candidates: object,
    max_results: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    include_worker_events: bool = False,
) -> dict[str, Any]:
    """Run one ranking fork for `kind` and return
    `{ids, reasoning, error, error_detail?}`.

    `error` is one of `None`, `"empty_query"`, `"timeout"`,
    `"dispatch_failed"`, `"parse_failed"`. `error_detail` (present only for
    `"dispatch_failed"`) carries the underlying exception message.
    `include_worker_events=True` additionally returns `_worker_events` (the
    fork's raw dispatch events) on a successful rank, for callers that show
    worker-turn provenance.

    Raises `KeyError` for an unregistered `kind`, `ValueError` for a
    structurally invalid `query`/`candidates`/`max_results` -- these are
    caller contract violations, not runtime ranking outcomes, so they are
    not folded into the `error` field.
    """
    rank_kind = get_kind(kind)
    if rank_kind is None:
        raise KeyError(f"unknown ai_rank kind: {kind!r}")
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results <= 0:
        raise ValueError("max_results must be a positive integer")
    max_results = min(max_results, _MAX_CANDIDATES)
    validated_candidates = _validate_candidates(candidates)

    query = query.strip()
    if len(query) > _MAX_QUERY_CHARS:
        raise ValueError(f"query exceeds {_MAX_QUERY_CHARS} chars")
    if not query:
        return {"ids": [], "reasoning": "", "error": "empty_query"}
    if not validated_candidates:
        return {"ids": [], "reasoning": "", "error": None}

    spec = _spec_for_kind(rank_kind)
    ctx = {"max_results": max_results, "candidates": validated_candidates}
    try:
        result = await asyncio.wait_for(provisioning.run(spec, query, ctx), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ids": [], "reasoning": "", "error": "timeout"}
    except Exception as exc:
        logger.exception("ai_rank.rank: provisioned dispatch failed kind=%s", kind)
        return {"ids": [], "reasoning": "", "error": "dispatch_failed", "error_detail": str(exc)}

    reported = result.value if result is not None else None
    if not isinstance(reported, dict) or reported.get("error"):
        return {"ids": [], "reasoning": "", "error": "parse_failed"}
    reported_ids = reported.get("ids")
    if not isinstance(reported_ids, list):
        return {"ids": [], "reasoning": "", "error": "parse_failed"}

    candidate_ids = {c["id"] for c in validated_candidates}
    seen: set[str] = set()
    ids: list[str] = []
    for rid in reported_ids:
        if isinstance(rid, str) and rid in candidate_ids and rid not in seen:
            seen.add(rid)
            ids.append(rid)
    ids = ids[:max_results]

    reasoning = reported.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, str) else ""
    out: dict[str, Any] = {"ids": ids, "reasoning": reasoning, "error": None}
    if include_worker_events:
        dispatch_result = getattr(result, "dispatch_result", None) or {}
        out["_worker_events"] = dispatch_result.get("events") or []
    return out
