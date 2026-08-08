import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SurfaceClient } from "../adapter/client";
import { fetchRuntimeProfiles as fetchRuntimeProfilesLegacy } from "../api";
import type { RuntimeProfilesSnapshotWire } from "../adapter/wire";
import type { ProviderRunner, ReasoningEffort, RuntimeProfile, RuntimeProfilesSnapshot } from "../types";
import {
  isProviderSocketOpen,
  subscribeProviderFrames,
  subscribeProviderSocketConnection,
} from "./useProviderSurfaceSocket";

// MIGRATED TO v2 (closure 3, ADR 0007 provider/auth plane migration): the
// two reasons the earlier pass kept this hook on legacy REST +
// `runtime_profiles_changed` eventBus are both closed now —
//  1. `descriptors.py`'s `RuntimeProfile`/`RuntimeProfilesSnapshot` read
//     models carry every field this hook's public `RuntimeProfilesSnapshot`
//     contract (`../types.ts`, consumed by App.tsx and others outside this
//     package's scope) needs: `name`/`deleted_at`/`created_at`/
//     `updated_at` per profile, plus `default_runtime_profile_id`/
//     `last_models`/`last_reasoning_efforts`/`deleted_providers` on the
//     snapshot itself — see `mapSnapshot` below for the field-for-field
//     mapping, nothing left unmapped/fabricated.
//  2. `provider.runtime_profiles_changed` (`runtime_profiles_api.
//     _broadcast_changed`) now fires UNCONDITIONALLY, on every mutation
//     path including `record_last_model`/`record_last_reasoning_effort`
//     (which carry no `provider_id`) — the same unconditional guarantee
//     the legacy WS broadcast already had, so cutting over is not a
//     narrowing of the live-update surface.
// The hook's own PUBLIC contract (`RuntimeProfilesState`) is UNCHANGED —
// App.tsx and every other consumer outside this package's scope keeps
// working with zero edits; only the data source moved.
//
// B6 ruling: resilience parity with every sibling provider-plane hook
// (`useProviderChanged`/`useModelsCatalogChanged`/`useProviderInstalls` all
// keep a legacy path reachable alongside v2, so a v2-only outage never
// blanks their data). `refresh()` below falls back to the legacy
// `GET /api/runtime-profiles` REST route (`backend/runtime_profiles_api.py`'s
// `list_runtime_profiles`, still live, still serving the exact
// `RuntimeProfilesSnapshot` shape) when the v2 REST fetch fails — this hook
// has no legacy WS to dual-subscribe to for the PUSH half (that's the
// pattern those siblings use; this hook's push half stays v2-only per the
// two reasons above, both still true), only the PULL half gets a fallback.

const client = new SurfaceClient();

function mapSnapshot(wire: RuntimeProfilesSnapshotWire): RuntimeProfilesSnapshot {
  return {
    runtime_profiles: wire.profiles.map((p) => ({
      id: p.runtime_profile_id,
      provider_id: p.provider_id,
      runner: p.runner as ProviderRunner,
      name: p.name,
      default_model: p.default_model,
      default_reasoning_effort: (p.default_reasoning_effort ?? "") as ReasoningEffort | "",
      created_at: p.created_at,
      updated_at: p.updated_at,
      deleted_at: p.deleted_at,
    })),
    default_runtime_profile_id: wire.default_runtime_profile_id,
    deleted_providers: wire.deleted_providers.map((d) => ({
      id: d.provider_id,
      name: d.name,
      nickname: d.nickname,
      kind: d.kind,
      deleted_at: d.deleted_at,
    })),
    last_models: wire.last_models,
    last_reasoning_efforts: wire.last_reasoning_efforts as Record<string, ReasoningEffort>,
  };
}

// Disposable localStorage projection of the backend snapshot (same pattern as
// `providerCache`): seeds the hook synchronously so offline session creation
// from an existing profile keeps working; every backend snapshot overwrites it.
// Contract unchanged by the v2 cutover — still the legacy-shaped
// `RuntimeProfilesSnapshot`, still consumed directly by
// `newSessionPromptDraft.test.tsx`/`new-session-voice-prompt.test.tsx`/
// `NewSessionModalOffline.test.tsx` outside this hook.
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

/** v2-backed runtime-profile state: v2 REST snapshot on mount (and on every
 * `refresh()`), converged by `runtime_profiles_changed` v2 frames (which
 * carry the same snapshot, so a push applies directly with no refetch) —
 * plus a refetch on the shared "providers" socket's OPEN transition, so a
 * mutation that lands in the brief window before that socket finishes
 * connecting (or after any drop/reconnect) is never silently missed
 * (mirrors the legacy hook's own `ws_connection_changed` "connected" ->
 * refresh() catch-up, using the v2 connection's own open event instead of
 * a second, legacy-only signal). */
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
    client
      .fetchRuntimeProfileDescriptors()
      .then((envelope) => {
        if (loadSeq.current !== seq) return;
        const snap = mapSnapshot(envelope);
        cacheRuntimeProfilesSnapshot(snap);
        setSnapshot(snap);
        setError(null);
      })
      .catch(() =>
        // Legacy REST fallback (B6 ruling, see module docstring) — the v2
        // route is unreachable; the legacy route returns the identical
        // snapshot shape directly, no `mapSnapshot` needed.
        fetchRuntimeProfilesLegacy()
          .then((snap) => {
            if (loadSeq.current !== seq) return;
            cacheRuntimeProfilesSnapshot(snap);
            setSnapshot(snap);
            setError(null);
          })
          .catch((fallbackErr) => {
            if (loadSeq.current !== seq) return;
            setError(fallbackErr instanceof Error ? fallbackErr.message : String(fallbackErr));
          }),
      )
      .finally(() => {
        if (loadSeq.current === seq) setLoading(false);
      });
  }, []);

  useEffect(() => {
    refresh();
    const unsubscribeFrames = subscribeProviderFrames((frame) => {
      if (frame.type !== "runtime_profiles_changed") return;
      // Push frames are authoritative snapshots — invalidate any in-flight
      // pull so a slow response can't overwrite the fresher push.
      loadSeq.current++;
      const snap = mapSnapshot(frame.snapshot);
      cacheRuntimeProfilesSnapshot(snap);
      setSnapshot(snap);
      setError(null);
      setLoading(false);
    });
    let wasOpen = isProviderSocketOpen();
    const unsubscribeConnection = subscribeProviderSocketConnection((open) => {
      if (open && !wasOpen) refresh();
      wasOpen = open;
    });
    return () => {
      unsubscribeFrames();
      unsubscribeConnection();
    };
  }, [refresh]);

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
