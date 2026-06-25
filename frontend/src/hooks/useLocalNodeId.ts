import { useEffect, useReducer } from "react";

import { extBackendBase } from "../extensionIds";

const machineNodesApi = () => extBackendBase("machineNodes");

// Module-level cache — every consumer shares the SAME local id.
// Single fetch on first hook mount; subsequent mounts read the cached
// value synchronously.
let _localId: string | null = null;
let _inFlight: Promise<void> | null = null;
const _subscribers = new Set<() => void>();

function _notify(): void {
  for (const fn of _subscribers) fn();
}

function _refetch(): Promise<void> {
  if (_inFlight) return _inFlight;
  _inFlight = (async () => {
    try {
      const r = await fetch(`${machineNodesApi()}/local_node_id`, {
        credentials: "include",
      });
      if (!r.ok) {
        _localId = "primary";
      } else {
        const data = (await r.json()) as { node_id?: string };
        _localId = data.node_id || "primary";
      }
    } catch {
      _localId = "primary";
    } finally {
      _inFlight = null;
      _notify();
    }
  })();
  return _inFlight;
}

/** Returns the local node's id from the machine-nodes extension.
 * Defaults to `"primary"` until the first fetch resolves so callers
 * never see `null`. Used to compute the `is_local` flag on
 * NodeSnapshot rows + render the "(host)" tag in pickers. */
export function useLocalNodeId(): string {
  const [, force] = useReducer((c: number) => c + 1, 0);
  useEffect(() => {
    _subscribers.add(force);
    if (_localId === null && !_inFlight) void _refetch();
    return () => {
      _subscribers.delete(force);
    };
  }, []);
  return _localId ?? "primary";
}
