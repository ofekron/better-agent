import { useEffect, useReducer } from "react";

import { extBackendBase } from "../extensionIds";
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
function _onRequested(ev: Event): void {
  const rec = (ev as CustomEvent<PendingNodeRegistration>).detail;
  if (!rec || !rec.node_id) return;
  const next = _pending.filter((p) => p.node_id !== rec.node_id);
  next.push(rec);
  _pending = next;
  _loaded = true;
  _notify();
}

// The request was approved or denied (here or in another tab) — drop it
// from the pending list so every open popup converges.
function _onResolved(ev: Event): void {
  const detail = (ev as CustomEvent<NodeRegistrationResolvedData>).detail;
  if (!detail || !detail.node_id) return;
  const next = _pending.filter((p) => p.node_id !== detail.node_id);
  if (next.length !== _pending.length) {
    _pending = next;
    _notify();
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("node_registration_requested", _onRequested);
  window.addEventListener("node_registration_resolved", _onResolved);
}

// Vite HMR: tear the old listeners down before the new module evaluates
// so we don't multiply patches per event.
if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    if (typeof window !== "undefined") {
      window.removeEventListener("node_registration_requested", _onRequested);
      window.removeEventListener("node_registration_resolved", _onResolved);
    }
  });
}

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
    if (authStatus === "authed" && !_loaded && !_inFlight) {
      void _refetch();
    }
    return () => {
      _subscribers.delete(force);
    };
  }, [authStatus]);
  return _pending;
}
