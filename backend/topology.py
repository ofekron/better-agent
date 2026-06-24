"""Multi-machine topology configuration.

Both the primary and worker-node backends read the SAME `topology.yaml`
file at boot. The file declares every machine that participates in this
Better Agent deployment: the primary (where canonical state lives and
the manager runs) and zero or more worker-nodes (machines that can host
remote workers).

Location: `$BETTER_CLAUDE_TOPOLOGY_PATH` (required on both primary and
node modes — no implicit search). Token for inter-node auth comes from
`$BETTER_CLAUDE_NODE_TOKEN` env var (also required; never embedded in
the file because the file is repo-checked-in).

File shape (`schema_version=1`):

    schema_version: 1
    primary:
      id: primary
      address: ws://primary.local:8001
      cwd_roots: ["/Users/ofek/code"]
    nodes:
      linux:
        address: linux.local:8002
        cwd_roots: ["/home/ofek/code"]

INVARIANT: `address` of `primary` is the URL nodes dial outward to
(so primary itself need not be reachable inbound from nodes' side beyond
this single endpoint). `address` under each `nodes.<id>` is metadata —
primary never dials nodes; nodes always dial primary.

Schema mismatch → raise at boot (per CLAUDE.md "wipe X to start fresh"
discipline — no migration scripts).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from env_compat import get_env

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class NodeSpec:
    id: str
    role: str  # "primary" | "worker_node"
    address: str
    cwd_roots: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Topology:
    schema_version: int
    primary: NodeSpec
    nodes: dict[str, NodeSpec]  # worker-nodes keyed by id; EXCLUDES primary

    def all_nodes(self) -> dict[str, NodeSpec]:
        """Every node including primary, keyed by id."""
        return {self.primary.id: self.primary, **self.nodes}

    def get(self, node_id: str) -> NodeSpec:
        nodes = self.all_nodes()
        if node_id not in nodes:
            raise KeyError(
                f"node_id {node_id!r} is not in topology.yaml — known nodes: "
                f"{sorted(nodes)}"
            )
        return nodes[node_id]


class TopologyError(RuntimeError):
    """Raised when topology.yaml is missing, malformed, or schema-mismatched."""


def _resolve_path() -> Path:
    raw = get_env("BETTER_CLAUDE_TOPOLOGY_PATH")
    if not raw:
        raise TopologyError(
            "BETTER_AGENT_TOPOLOGY_PATH or BETTER_CLAUDE_TOPOLOGY_PATH env var is required (no implicit "
            "search). Point it at your topology.yaml."
        )
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise TopologyError(
            f"BETTER_AGENT_TOPOLOGY_PATH or BETTER_CLAUDE_TOPOLOGY_PATH must be absolute, got {raw!r}"
        )
    return p


def _parse_node(node_id: str, role: str, raw: dict) -> NodeSpec:
    if not isinstance(raw, dict):
        raise TopologyError(
            f"node {node_id!r}: expected mapping, got {type(raw).__name__}"
        )
    address = raw.get("address")
    if not isinstance(address, str) or not address:
        raise TopologyError(
            f"node {node_id!r}: missing or non-string `address`"
        )
    cwd_roots = raw.get("cwd_roots") or []
    if not isinstance(cwd_roots, list) or not all(
        isinstance(r, str) and r.startswith("/") for r in cwd_roots
    ):
        raise TopologyError(
            f"node {node_id!r}: `cwd_roots` must be a list of absolute paths"
        )
    return NodeSpec(
        id=node_id,
        role=role,
        address=address,
        cwd_roots=tuple(cwd_roots),
    )


_cache: Optional[Topology] = None


def load_topology(*, force_reload: bool = False) -> Topology:
    """Read and validate topology.yaml. Cached per-process — pass
    `force_reload=True` from tests to bypass.

    Raises `TopologyError` on missing env var, missing file, malformed
    yaml, or schema_version mismatch."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache

    path = _resolve_path()
    if not path.exists():
        raise TopologyError(f"topology.yaml not found at {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise TopologyError(f"topology.yaml parse failed: {e}") from e

    if not isinstance(raw, dict):
        raise TopologyError(
            f"topology.yaml: top-level must be a mapping, got "
            f"{type(raw).__name__}"
        )

    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise TopologyError(
            f"topology.yaml schema_version={schema_version!r} but this "
            f"backend expects {SCHEMA_VERSION}. Wipe + recreate the file "
            f"or update the backend."
        )

    primary_raw = raw.get("primary")
    if not isinstance(primary_raw, dict):
        raise TopologyError("topology.yaml: missing `primary` mapping")
    primary_id = primary_raw.get("id") or "primary"
    primary = _parse_node(primary_id, "primary", primary_raw)

    nodes: dict[str, NodeSpec] = {}
    for node_id, node_raw in (raw.get("nodes") or {}).items():
        if node_id == primary.id:
            raise TopologyError(
                f"topology.yaml: node id {node_id!r} duplicates primary's id"
            )
        nodes[node_id] = _parse_node(node_id, "worker_node", node_raw)

    _cache = Topology(
        schema_version=schema_version,
        primary=primary,
        nodes=nodes,
    )
    return _cache


def local_node_id() -> str:
    """Resolve which node THIS process is via `BETTER_CLAUDE_NODE_ID`
    env var. Set by `bc start --as <id>` / `bc node --as <id>` in the
    CLI dispatcher. Defaults to `"primary"` when unset so single-machine
    setups don't have to plumb the env var.

    INVARIANT: the value must appear in topology.yaml (raised if not)."""
    node_id = get_env("BETTER_CLAUDE_NODE_ID") or "primary"
    topology = load_topology()
    if node_id not in topology.all_nodes():
        raise TopologyError(
            f"BETTER_AGENT_NODE_ID/BETTER_CLAUDE_NODE_ID={node_id!r} is not declared in "
            f"topology.yaml — known: {sorted(topology.all_nodes())}"
        )
    return node_id


def remove_node(node_id: str) -> bool:
    """Remove a static worker-node from topology.yaml and invalidate cache.
    Cannot remove the primary. Returns True if a node was removed.
    Raises TopologyError on malformed YAML so the caller can distinguish
    'not found' from 'file is corrupt'."""
    global _cache
    path = _resolve_path()
    if not path.exists():
        return False

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise TopologyError(f"topology.yaml parse failed during remove: {e}") from e

    if not isinstance(raw, dict):
        raise TopologyError("topology.yaml: top-level must be a mapping")

    nodes = raw.get("nodes")
    if not isinstance(nodes, dict) or node_id not in nodes:
        return False

    del nodes[node_id]
    # Atomic write: write to temp file in same dir, then os.replace
    content = yaml.dump(raw, default_flow_style=False, sort_keys=False)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=".topology-", suffix=".tmp"
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _cache = None
    return True


def node_token() -> str:
    """Shared secret for the LEGACY static-topology auth path. Required
    only when a node is declared in topology.yaml (dynamic nodes use the
    approval flow + per-node secret instead — see node_registry_store)."""
    tok = get_env("BETTER_CLAUDE_NODE_TOKEN")
    if not tok:
        raise TopologyError(
            "BETTER_AGENT_NODE_TOKEN or BETTER_CLAUDE_NODE_TOKEN env var is required (shared secret "
            "for node↔primary auth; both ends must agree)."
        )
    return tok


def node_token_optional() -> Optional[str]:
    """Like `node_token()` but returns None when unset instead of raising.
    Used by the primary's node_link to support deployments that rely
    entirely on the dynamic approval flow and set no shared token."""
    tok = get_env("BETTER_CLAUDE_NODE_TOKEN")
    return tok or None
