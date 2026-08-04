import { useEffect, useReducer } from "react";

import { extBackendBase } from "../extensionIds";
import { eventBus } from "../lib/eventBus";
import type {
  NodeRegistrationResolvedData,
  PendingNodeRegistration,
} from "../types";

const machineNodesApi = () => extBackendBase("machineNodes");

// Module-level shared state — mirrors useMachines. Every consumer reads
// the SAME `_state` so N mounts fire ONE pending-node GET, and the
// WS-driven patch path runs even when no component is currently mounted
// (the next mount sees fresh state without a refetch). Authoritative
// snapshot lives on the backend; this module only reflects it.
let _pending: PendingNodeRegistration[] = [];
let _loaded = false;
let _authed = false;
let _inFlight: Promise<void> | null = null;
const _subscribers = new Set<() => void>();

function _notify(): void {
  for (const fn of _subscribers) fn();
}

function _refetch(): Promise<void> {
  if (_inFlight) return _inFlight;
  _inFlight = (async () => {
    try {
      const r = await fetch(`${machineNodesApi()}/pending_nodes`, {
        credentials: "include",
      });
      const data = r.ok ? await r.json() : { pending_nodes: [] };
      _pending = Array.isArray(data?.pending_nodes)
        ? (data.pending_nodes as PendingNodeRegistration[])
        : [];
      _loaded = true;
    } catch {
      // Leave prior state on transient failure; a later WS frame or
      // remount refetches.
    } finally {
      _inFlight = null;
      _notify();
    }
  })();
  return _inFlight;
}

// A brand-new node is awaiting approval. The payload IS the public
// record (node_id, address, cwd_roots, fingerprint, status, timestamps),
// so we can upsert it directly without a refetch — but a re-dial of an
// already-listed node (same node_id, possibly new fingerprint) must
// REPLACE the stale row.
function _onRequested(rec: PendingNodeRegistration): void {
  if (!rec || !rec.node_id) return;
  const next = _pending.filter((p) => p.node_id !== rec.node_id);
  next.push(rec);
  _pending = next;
  _loaded = true;
  _notify();
}

// The request was approved or denied (here or in another tab) — drop it
// from the pending list so every open popup converges.
function _onResolved(detail: NodeRegistrationResolvedData): void {
  if (!detail || !detail.node_id) return;
  const next = _pending.filter((p) => p.node_id !== detail.node_id);
  if (next.length !== _pending.length) {
    _pending = next;
    _notify();
  }
}

// `node_registration_requested` is a live WS frame with no reconnect
// replay, so anything emitted while this client was disconnected is
// lost. Re-pull the snapshot whenever the socket comes back — including
// when the first load never landed, which is exactly the offline case.
// Gated on the mounted hook's auth status rather than on `_loaded`: a
// thrown (offline) first fetch leaves `_loaded` false forever.
function _onConnectionChanged({ connected }: { connected: boolean }): void {
  if (connected && _authed) void _refetch();
}

const _offRequested = eventBus.subscribe("node_registration_requested", _onRequested);
const _offResolved = eventBus.subscribe("node_registration_resolved", _onResolved);
const _offConnection = eventBus.subscribe("ws_connection_changed", _onConnectionChanged);

// Vite HMR: tear the old subscriptions down before the new module evaluates
// so we don't multiply patches per event. The dispose callback only fires
// when the dev server hot-replaces this module — unreachable in tests and
// production builds, so it is excluded from coverage.
/* v8 ignore start */
if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    _offRequested();
    _offResolved();
    _offConnection();
  });
}
/* v8 ignore stop */

export async function approveNodeRegistration(nodeId: string): Promise<void> {
  const r = await fetch(
    `${machineNodesApi()}/pending_nodes/${encodeURIComponent(nodeId)}/approve`,
    { method: "POST", credentials: "include" },
  );
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body?.detail || `approve failed (${r.status})`);
  }
  // The backend emits node_registration_resolved → _onResolved removes
  // it. Patch locally too so the acting tab doesn't wait a round-trip.
  _pending = _pending.filter((p) => p.node_id !== nodeId);
  _notify();
}

export async function denyNodeRegistration(nodeId: string): Promise<void> {
  const r = await fetch(
    `${machineNodesApi()}/pending_nodes/${encodeURIComponent(nodeId)}/deny`,
    { method: "POST", credentials: "include" },
  );
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body?.detail || `deny failed (${r.status})`);
  }
  _pending = _pending.filter((p) => p.node_id !== nodeId);
  _notify();
}

/** Reflects worker-nodes awaiting operator approval. Pull-then-push:
 * REST snapshot on first mount, then WS-driven patches. Per CLAUDE.md
 * state-ownership rule, this hook MUST NOT persist anything locally —
 * the backend's `pending_node_registrations` store is the only source
 * of truth. */
export function usePendingNodeRegistrations(
  authStatus?: string,
): PendingNodeRegistration[] {
  const [, force] = useReducer((c: number) => c + 1, 0);
  useEffect(() => {
    _subscribers.add(force);
    _authed = authStatus === "authed";
    if (authStatus === "authed" && !_loaded && !_inFlight) {
      void _refetch();
    }
    return () => {
      _subscribers.delete(force);
    };
  }, [authStatus]);
  return _pending;
}
