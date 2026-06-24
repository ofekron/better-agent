"""Slim FastAPI app for worker-node mode.

A worker-node process runs `uvicorn main_node:app` (not `main:app`).
The node:

  - Loads topology + connects out to primary via `node_client`.
  - Hosts the local `ClaudeProvider` so it can spawn claude
    subprocesses on demand (driven by `spawn_run` from primary).
  - Reverse-proxies `/api/internal/delegate` to primary so nested
    delegations from manager-mode workers running on the node land
    on primary's canonical state.

The node has NO session_store, NO worker_store, NO pending_approvals
hosting, NO manager session — those live on primary.

Required env vars:
  BETTER_CLAUDE_TOPOLOGY_PATH   — absolute path to topology.yaml
                                  (supplies the primary address to dial)

Optional env vars (override the persisted node_identity.json):
  BETTER_CLAUDE_NODE_ID         — pin this node's id (default: hostname)
  BETTER_CLAUDE_NODE_TOKEN      — pin the node secret (default: generated
                                  once and persisted). A brand-new node
                                  is approved by an operator in the
                                  primary UI rather than via a shared token.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import httpx

from backend_instance_lock import (
    acquire_backend_instance_lock,
    release_backend_instance_lock,
)
from paths import ba_home
import config_store

config_store.apply_env_vars()  # apply provider env vars before SDK-touching imports

from provider import default_provider, load_all_providers  # noqa: E402
from topology import load_topology  # noqa: E402
import node_identity  # noqa: E402
from node_client import NodeClient, set_singleton  # noqa: E402


# See main.py — log dir is captured at module-load by design;
# the `FileHandler` binds a single Path. State storage uses lazy
# helpers (A12); logging doesn't.
_log_dir = ba_home() / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_dir / "node.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


app = FastAPI(title=f"Better Agent — node {os.environ.get('BETTER_CLAUDE_NODE_ID', '?')}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_client: NodeClient | None = None


@app.on_event("startup")
async def _on_startup() -> None:
    global _client

    acquire_backend_instance_lock()

    # Topology supplies the primary address to dial. The node itself no
    # longer needs to be declared there (dynamic approval flow) nor share
    # a token — it presents its persistent identity secret and waits for
    # an operator to approve it on primary.
    topology = load_topology()
    identity = node_identity.load_or_create()
    me = identity.node_id
    if me == topology.primary.id:
        raise RuntimeError(
            f"main_node: node id {me!r} matches primary's id — primary "
            f"should run main.py, not main_node.py (set BETTER_CLAUDE_NODE_ID "
            f"to a distinct value)"
        )
    logger.info("main_node: starting as node=%s primary=%s",
                me, topology.primary.address)

    try:
        load_all_providers()
    except Exception:
        logger.exception("main_node: load_all_providers failed")
    try:
        default_provider().prune_old_runs()
    except Exception:
        logger.exception("main_node: prune_old_runs failed")

    _client = NodeClient()
    set_singleton(_client)
    await _client.start()


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    if _client is not None:
        await _client.stop()
    release_backend_instance_lock()


@app.get("/healthz")
async def healthz() -> dict:
    topology = load_topology()
    return {
        "node_id": node_identity.load_or_create().node_id,
        "primary": topology.primary.address,
        "default_provider": default_provider().id,
    }


# ============================================================================
# Reverse-proxy canonical-state internal endpoints → primary
# ----------------------------------------------------------------------------
# Workers running on the node HTTP-loopback to their BETTER_CLAUDE_BACKEND_URL,
# which we set to the node's localhost so worker MCP servers (file_editor etc.)
# work without needing WAN paths. For the MCP calls that need canonical state
# on primary — ask(fork)'s fork engine (/api/internal/ask-fork) and the
# delegate_task router (/api/internal/delegate-task) — the node forwards to
# primary, preserving the worker's internal_token (which primary minted and
# shipped to the node in spawn_run).
# ============================================================================
async def _proxy_to_primary(request: Request, path: str) -> Response:
    topology = load_topology()
    primary_addr = topology.primary.address.rstrip("/")
    # Convert ws:// → http:// for the HTTP forward.
    if primary_addr.startswith("ws://"):
        primary_addr = "http://" + primary_addr[len("ws://"):]
    elif primary_addr.startswith("wss://"):
        primary_addr = "https://" + primary_addr[len("wss://"):]
    upstream = f"{primary_addr}{path}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    body = await request.body()

    async with httpx.AsyncClient(timeout=None) as client:
        upstream_resp = await client.post(upstream, headers=headers, content=body)

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers={
            k: v for k, v in upstream_resp.headers.items()
            if k.lower() not in ("content-encoding", "transfer-encoding")
        },
        media_type=upstream_resp.headers.get("content-type"),
    )


@app.api_route("/api/internal/ask-fork", methods=["POST"])
async def proxy_ask_fork(request: Request) -> Response:
    return await _proxy_to_primary(request, "/api/internal/ask-fork")


@app.api_route("/api/internal/delegate-task", methods=["POST"])
async def proxy_delegate_task(request: Request) -> Response:
    return await _proxy_to_primary(request, "/api/internal/delegate-task")
