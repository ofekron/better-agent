import { useEffect, useReducer } from "react";

import { API } from "../api";
import type { NodeSnapshot, NodeStateChangedData } from "../types";

const MACHINE_NODES_API = `${API}/api/extensions/ofek-dev.machine-nodes/backend`;

interface MachinesState {
  /** Snapshot of all machines (primary + worker_nodes) from the
   * machine-nodes extension. Empty list = single-machine deploy (no
   * topology.yaml configured) OR initial fetch in flight (see
   * `loading`). Authoritative state lives on the backend; this
   * module patches per-machine `state` / `last_seen` from
   * `node_state_changed` WS frames. */
  machines: NodeSnapshot[];
  /** True from module load until the first node snapshot fetch resolves
   * (success or failure). Lets callers distinguish "loading" from
   * "no machines configured" — both render `machines.length === 0`. */
  loading: boolean;
}

// Module-level shared state — every useMachines() consumer reads the
// SAME `_state` and renders against the SAME subscribers set. Replaces
// per-hook component state so N components mounting useMachines fire
// exactly ONE node snapshot GET, not N parallel ones.
let _state: MachinesState = { machines: [], loading: true };
let _inFlight: Promise<void> | null = null;
const _subscribers = new Set<() => void>();

function _notify(): void {
  for (const fn of _subscribers) fn();
}

function _refetch(): Promise<void> {
  if (_inFlight) return _inFlight;
  _inFlight = (async () => {
    try {
      const r = await fetch(`${MACHINE_NODES_API}/nodes`, { credentials: "include" });
      const data = r.ok ? await r.json() : [];
      _state = {
        machines: Array.isArray(data) ? (data as NodeSnapshot[]) : [],
        loading: false,
      };
    } catch {
      _state = { machines: [], loading: false };
    } finally {
      _inFlight = null;
      _notify();
    }
  })();
  return _inFlight;
}

// Backend WS push: a worker-node transitioned connected/disconnected.
// Listener attached ONCE at module load (not per-hook) so the patch
// path runs even when no component currently has the hook mounted —
// the next mount sees fresh state without a refetch.
function _onNodeStateChanged(ev: Event): void {
  const detail = (ev as CustomEvent<NodeStateChangedData>).detail;
  if (!detail || !detail.node_id) return;
  const idx = _state.machines.findIndex((m) => m.id === detail.node_id);
  if (idx === -1) {
    // Unknown node appearing live — refetch so we learn its metadata
    // (role, address, cwd_roots). State alone isn't a complete row.
    void _refetch();
    return;
  }
  const next = _state.machines.slice();
  next[idx] = {
    ...next[idx],
    state: detail.state,
    // INVARIANT: `last_seen` is the timestamp the backend last heard
    // from this node. On a `disconnected` transition the backend has
    // already wiped the live conn (so the payload carries `null`);
    // we PRESERVE the prior heartbeat so the UI can render "last
    // seen N min ago" instead of erasing the timestamp. On a
    // `connected` transition the backend sends a fresh value which
    // overwrites the stale one.
    last_seen: detail.last_seen ?? next[idx].last_seen,
  };
  _state = { ..._state, machines: next };
  _notify();
}

if (typeof window !== "undefined") {
  window.addEventListener("node_state_changed", _onNodeStateChanged);
}

// Vite HMR: re-evaluating this module would add a SECOND listener that
// can't be removed, multiplying patches per event. Dispose hook tears
// the old listener down before the new module evaluates.
if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    if (typeof window !== "undefined") {
      window.removeEventListener("node_state_changed", _onNodeStateChanged);
    }
  });
}

/** Delete a node from the topology. Returns true on success. */
export async function deleteNode(nodeId: string): Promise<boolean> {
  try {
    const r = await fetch(`${MACHINE_NODES_API}/nodes/${encodeURIComponent(nodeId)}`, {
      method: "DELETE",
      credentials: "include",
    });
    if (!r.ok) return false;
    await _refetch();
    return true;
  } catch {
    return false;
  }
}

/** Restart a connected worker-node. Returns true on success. */
export async function restartNode(nodeId: string): Promise<boolean> {
  try {
    const r = await fetch(
      `${MACHINE_NODES_API}/nodes/${encodeURIComponent(nodeId)}/restart`,
      { method: "POST", credentials: "include" },
    );
    return r.ok;
  } catch {
    return false;
  }
}

/** Reflects the backend's multi-machine topology + live connection
 * state. Pull-then-push: REST snapshot on first mount, then WS-driven
 * incremental patches. Per CLAUDE.md state-ownership rule, this hook
 * MUST NOT persist anything locally — backend is the only source of
 * truth. */
export function useMachines(authStatus?: string): MachinesState {
  const [, force] = useReducer((c: number) => c + 1, 0);
  useEffect(() => {
    _subscribers.add(force);
    if (authStatus === "authed" && _state.loading && !_inFlight) {
      void _refetch();
    }
    return () => {
      _subscribers.delete(force);
    };
  }, [authStatus]);
  return _state;
}
