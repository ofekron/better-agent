from __future__ import annotations

import asyncio

import extension_store
from fastapi import HTTPException


async def node_op(node_id: str, method: str, params: dict):
    """REST-endpoint wrapper around `node_rpc_handlers.call_local_or_remote`
    that translates plain exceptions to FastAPI HTTPExceptions.

    Error translation:
      NodeOffline          → 503
      asyncio.TimeoutError → 504
      RuntimeError         → 502 (remote handler raised)
      FileNotFoundError    → 404
      FileExistsError      → 409
      PermissionError      → 403
      ValueError           → 400
    """
    if node_id != "primary":
        nodes_not_ready = extension_store.runtime_not_ready_message(
            extension_store.extension_id_for_role('machine-nodes')
        )
        if nodes_not_ready is not None:
            raise HTTPException(status_code=404, detail=nodes_not_ready)
    import node_link
    from node_rpc_handlers import call_local_or_remote
    try:
        return await call_local_or_remote(node_id, method, params)
    except HTTPException:
        raise
    except node_link.NodeOffline as e:
        raise HTTPException(status_code=503, detail=str(e))
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"node {node_id!r} did not respond within timeout",
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # RuntimeErrors come from the remote handler → 502.
        raise HTTPException(status_code=502, detail=str(e))
