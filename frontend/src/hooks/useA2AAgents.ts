import { useCallback, useRef, useState } from "react";
import { fetchA2AAgents } from "../api";
import type { A2AAgentsSnapshot } from "../types";
import { useBusEffect } from "./useBusEffect";

// `a2a_registry_changed` frames carry the snapshot; `ws_connection_changed`
// (connected) re-pulls after an offline window in which pushes were missed.
const TOPICS = ["a2a_registry_changed", "ws_connection_changed"] as const;

function isSnapshot(value: unknown): value is A2AAgentsSnapshot {
  return (
    !!value
    && typeof value === "object"
    && Array.isArray((value as A2AAgentsSnapshot).agents)
  );
}

export interface A2AAgentsState {
  snapshot: A2AAgentsSnapshot | null;
  agents: A2AAgentsSnapshot["agents"];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

/** Backend-owned A2A registry state: REST snapshot on mount, converged by
 * `a2a_registry_changed` WS frames (which carry the same snapshot, so a
 * push applies directly with no refetch). */
export function useA2AAgents(): A2AAgentsState {
  const [snapshot, setSnapshot] = useState<A2AAgentsSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const loadSeq = useRef(0);

  const refresh = useCallback(() => {
    const seq = ++loadSeq.current;
    setLoading(true);
    fetchA2AAgents()
      .then((snap) => {
        if (loadSeq.current !== seq) return;
        setSnapshot(snap);
        setError(null);
      })
      .catch((e) => {
        if (loadSeq.current !== seq) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (loadSeq.current === seq) setLoading(false);
      });
  }, []);

  useBusEffect(
    TOPICS,
    (payload) => {
      if (isSnapshot(payload)) {
        loadSeq.current++;
        setSnapshot(payload);
        setError(null);
        setLoading(false);
        return;
      }
      if (payload && typeof payload === "object" && "connected" in payload) {
        if ((payload as { connected?: boolean }).connected) refresh();
        return;
      }
      refresh();
    },
    { onMount: true },
  );

  return {
    snapshot,
    agents: snapshot?.agents ?? [],
    loading,
    error,
    refresh,
  };
}
