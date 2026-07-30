import { useCallback, useMemo, useRef, useState } from "react";
import { fetchRuntimeProfiles } from "../api";
import type { RuntimeProfile, RuntimeProfilesSnapshot } from "../types";
import { useBusEffect } from "./useBusEffect";

// `runtime_profiles_changed` frames carry the snapshot; `ws_connection_changed`
// (connected) re-pulls after an offline window in which pushes were missed.
const TOPICS = ["runtime_profiles_changed", "ws_connection_changed"] as const;

// Disposable localStorage projection of the backend snapshot (same pattern as
// `providerCache`): seeds the hook synchronously so offline session creation
// from an existing profile keeps working; every backend snapshot overwrites it.
const CACHE_KEY = "better-agent-runtime-profiles-cache";
const CACHE_VERSION = 1;

function isSnapshot(value: unknown): value is RuntimeProfilesSnapshot {
  return (
    !!value
    && typeof value === "object"
    && Array.isArray((value as RuntimeProfilesSnapshot).runtime_profiles)
  );
}

export function readRuntimeProfilesCache(): RuntimeProfilesSnapshot | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { version?: number; snapshot?: unknown };
    if (parsed.version !== CACHE_VERSION || !isSnapshot(parsed.snapshot)) return null;
    return parsed.snapshot;
  } catch {
    return null;
  }
}

export function cacheRuntimeProfilesSnapshot(snapshot: RuntimeProfilesSnapshot): void {
  try {
    localStorage.setItem(
      CACHE_KEY,
      JSON.stringify({ version: CACHE_VERSION, snapshot }),
    );
  } catch {
    // The cache is disposable; storage failures must not block online state.
  }
}

export interface RuntimeProfilesState {
  /** Full backend snapshot (tombstones included). Null until first load. */
  snapshot: RuntimeProfilesSnapshot | null;
  /** Live (non-deleted) profiles — what pickers offer. */
  profiles: RuntimeProfile[];
  defaultProfileId: string | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

/** Backend-owned runtime-profile state: REST snapshot on mount, converged by
 * `runtime_profiles_changed` WS frames (which carry the same snapshot, so a
 * push applies directly with no refetch). */
export function useRuntimeProfiles(): RuntimeProfilesState {
  const [snapshot, setSnapshot] = useState<RuntimeProfilesSnapshot | null>(
    () => readRuntimeProfilesCache(),
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const loadSeq = useRef(0);

  const refresh = useCallback(() => {
    const seq = ++loadSeq.current;
    setLoading(true);
    fetchRuntimeProfiles()
      .then((snap) => {
        if (loadSeq.current !== seq) return;
        cacheRuntimeProfilesSnapshot(snap);
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
        // Push frames are authoritative snapshots — invalidate any in-flight
        // pull so a slow response can't overwrite the fresher push.
        loadSeq.current++;
        cacheRuntimeProfilesSnapshot(payload);
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

  const profiles = useMemo(
    () => (snapshot?.runtime_profiles ?? []).filter((p) => !p.deleted_at),
    [snapshot],
  );

  return {
    snapshot,
    profiles,
    defaultProfileId: snapshot?.default_runtime_profile_id ?? null,
    loading,
    error,
    refresh,
  };
}
