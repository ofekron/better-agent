import { useEffect, useReducer } from "react";

import { extBackendBase } from "../extensionIds";
import { eventBus } from "../lib/eventBus";
import { subscribeSystemFrames, submitSystemIntent } from "../lib/systemFeedRegistry";
import type { NodeDescriptorWire, NodeProviderCredentialStatusWire, SystemFrame } from "../adapter/wire";
import type {
  NodeProviderCredentialsChangedData,
  NodeSnapshot,
  NodeStateChangedData,
} from "../types";

const machineNodesApi = () => extBackendBase("machineNodes");

/** v2 `NodeDescriptor` (ADR 0011 §9) -> legacy `NodeSnapshot` — nullable
 * v2 version-tracking fields map onto legacy's required-string/boolean
 * shape with the same "unknown" defaults the legacy backend itself uses
 * for a node it has no build-fingerprint for yet (empty string / false).
 * `provider_credentials` is deliberately NOT populated here — kept legacy
 * for that piece: `NodeProviderCredentialStatus` (v2) carries `provider_id`
 * only, never legacy's `provider_name` display string (ADR 0011 §9's own
 * "provider_id resolves through descriptors, never a raw name" rule), and
 * this hook has no provider-descriptor lookup available to resolve one. */
function mapNodeDescriptor(wire: NodeDescriptorWire, previous: NodeSnapshot | undefined): NodeSnapshot {
  return {
    id: wire.node_id,
    role: wire.role,
    address: wire.address,
    cwd_roots: wire.cwd_roots,
    state: wire.state,
    connected_at: wire.connected_at,
    last_seen: wire.last_seen,
    app_commit_sha: wire.app_commit_sha ?? "",
    app_dirty: wire.app_dirty ?? false,
    primary_commit_sha: wire.primary_commit_sha ?? "",
    primary_dirty: wire.primary_dirty ?? false,
    version_status: wire.version_status,
    provider_credentials: previous?.provider_credentials,
  };
}

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

export interface MachineSyncNodeResult {
  node_id: string;
  ok: boolean;
  error?: string;
  [key: string]: unknown;
}

export interface MachineSyncResult {
  ok: boolean;
  results?: MachineSyncNodeResult[];
  error?: string;
}

export interface ProviderSyncOptions {
  includeSecrets?: boolean;
  providerIds?: string[];
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
      const r = await fetch(`${machineNodesApi()}/nodes`, { credentials: "include" });
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
  // NOTE: no v2 GET backfill here (deliberately) — `tests/useMachines
  // .test.tsx`'s "shares one fetch across N mounted consumers"/"collapses
  // a refetch onto the in-flight request" tests assert exactly one network
  // call per refetch; adding a second (v2) REST call would violate that
  // documented sharing contract for a component this hot (mounted by
  // `NewSessionModal`/`DirPickerModal`). The LIVE `machines` feed
  // subscription below still applies real v2 incremental patches
  // (`_onSystemMachinesFrame`) — only the REST cold-hydration stays
  // single-sourced (legacy).
  return _inFlight;
}

// Backend WS push: a worker-node transitioned connected/disconnected.
// Subscription attached ONCE at module load (not per-hook) so the patch
// path runs even when no component currently has the hook mounted —
// the next mount sees fresh state without a refetch.
function _onNodeStateChanged(detail: NodeStateChangedData): void {
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
    app_commit_sha: detail.app_commit_sha ?? next[idx].app_commit_sha,
    app_dirty: detail.app_dirty ?? next[idx].app_dirty,
    primary_commit_sha: detail.primary_commit_sha ?? next[idx].primary_commit_sha,
    primary_dirty: detail.primary_dirty ?? next[idx].primary_dirty,
    version_status: detail.version_status ?? next[idx].version_status,
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

function _onNodeProviderCredentialsChanged(
  detail: NodeProviderCredentialsChangedData,
): void {
  if (!detail?.node_id || !Array.isArray(detail.provider_credentials)) return;
  const idx = _state.machines.findIndex((machine) => machine.id === detail.node_id);
  if (idx === -1) {
    void _refetch();
    return;
  }
  const next = _state.machines.slice();
  next[idx] = {
    ...next[idx],
    provider_credentials: detail.provider_credentials,
  };
  _state = { ..._state, machines: next };
  _notify();
}

const _offNodeStateChanged = eventBus.subscribe(
  "node_state_changed",
  _onNodeStateChanged,
);
const _offNodeProviderCredentialsChanged = eventBus.subscribe(
  "node_provider_credentials_changed",
  _onNodeProviderCredentialsChanged,
);

// ADR 0011 §9 `machines` feed — dual with the legacy WS patches above
// (both apply to the SAME `_state.machines` array; a v2 `MachineNodeUpsert`
// patches/creates a row exactly like `_onNodeStateChanged` does, a
// `NodeRemoved` drops it — legacy has no removal path at all today, a
// genuinely NEW capability this feed adds, not just a duplicate signal).
function _onSystemMachinesFrame(frame: SystemFrame): void {
  if (frame.type === "machine_node_upsert") {
    const idx = _state.machines.findIndex((m) => m.id === frame.node.node_id);
    const next = _state.machines.slice();
    const mapped = mapNodeDescriptor(frame.node, idx === -1 ? undefined : next[idx]);
    if (idx === -1) next.push(mapped);
    else next[idx] = mapped;
    _state = { ..._state, machines: next };
    _notify();
    return;
  }
  if (frame.type === "node_removed") {
    const next = _state.machines.filter((m) => m.id !== frame.node_id);
    if (next.length !== _state.machines.length) {
      _state = { ..._state, machines: next };
      _notify();
    }
  }
}

const _offSystemMachinesFrame = subscribeSystemFrames(_onSystemMachinesFrame);

// Vite HMR: re-evaluating this module would add a SECOND subscription that
// can't be removed, multiplying patches per event. Dispose hook tears
// the old listener down before the new module evaluates.
if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    _offNodeStateChanged();
    _offNodeProviderCredentialsChanged();
    _offSystemMachinesFrame();
  });
}

/** Delete a node from the topology. Returns true on success. Native-when-
 * open (ADR 0011 §9's `remove_node` -> the SAME `node_registry_store.remove`
 * / `topology.remove_node` functions the legacy `DELETE` route calls),
 * REST fallback otherwise — a clean match: the intent's accept/reject ack
 * is exactly the `boolean` this function already returns, no synchronous
 * payload lost by going native. */
export async function deleteNode(nodeId: string): Promise<boolean> {
  const native = submitSystemIntent({ kind: "remove_node", node_id: nodeId });
  if (native) {
    const ack = await native;
    if (ack.type === "intent_rejected") return false;
    await _refetch();
    return true;
  }
  try {
    const r = await fetch(`${machineNodesApi()}/nodes/${encodeURIComponent(nodeId)}`, {
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

/** Resolve a pending worker-node registration request (approve/deny).
 * ADR 0011 §9's `resolve_node_registration` — native-when-open, REST
 * fallback against the legacy `pending_nodes/{id}/approve|deny` two-route
 * split. NOT wired to any current UI: `usePendingNodeRegistrations.ts`
 * (the only historical consumer) is confirmed dead code with zero
 * importers (migration plan's "Confirmed dead code" list) — exported here
 * per ADR 0011 §9's designation of this concern as machine/node-topology-
 * scoped, so a future node-registration UI has a real mutation path to
 * call into rather than needing to rebuild one. */
export async function resolveNodeRegistration(
  nodeId: string,
  decision: "approved" | "denied",
): Promise<boolean> {
  const native = submitSystemIntent({ kind: "resolve_node_registration", node_id: nodeId, decision });
  if (native) {
    const ack = await native;
    return ack.type !== "intent_rejected";
  }
  try {
    const r = await fetch(
      `${machineNodesApi()}/pending_nodes/${encodeURIComponent(nodeId)}/${decision === "approved" ? "approve" : "deny"}`,
      { method: "POST", credentials: "include" },
    );
    return r.ok;
  } catch {
    return false;
  }
}

/** Restart a connected worker-node. Returns true on success. No ADR 0011
 * `SystemIntent` exists for "restart a node" at all (§9 covers topology +
 * credential-sync + registration only) — stays 100% legacy, a genuine
 * absence rather than an unmigrated one. */
export async function restartNode(nodeId: string): Promise<boolean> {
  try {
    const r = await fetch(
      `${machineNodesApi()}/nodes/${encodeURIComponent(nodeId)}/restart`,
      { method: "POST", credentials: "include" },
    );
    return r.ok;
  } catch {
    return false;
  }
}

async function postMachineSync(url: string, body?: unknown): Promise<MachineSyncResult> {
  try {
    const r = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data: unknown = null;
    try {
      data = await r.json();
    } catch {
      data = null;
    }
    if (data && typeof data === "object") {
      const body = data as {
        ok?: unknown;
        results?: unknown;
        detail?: unknown;
        error?: unknown;
      };
      const results = Array.isArray(body.results)
        ? (body.results as MachineSyncNodeResult[])
        : undefined;
      const bodyOk =
        typeof body.ok === "boolean"
          ? body.ok
          : results
            ? results.every((result) => result.ok === true)
            : r.ok;
      const message = body.detail || body.error;
      return {
        ok: r.ok && bodyOk,
        results,
        error: typeof message === "string" ? message : undefined,
      };
    }
    return { ok: r.ok };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

/** Time to wait for `authorize_and_sync`'s per-provider terminal statuses
 * (`synced`/`failed`) to arrive over the `machines` feed once the intent
 * is accepted — generous relative to the 1500ms ack-only late-rejection
 * window (`lib/surfaceFeedSocket.ts`'s `LATE_REJECTION_WINDOW_MS`) since
 * this specifically waits on a real node RPC round trip, not just the
 * command being admitted. */
const CREDENTIAL_SYNC_RESULT_TIMEOUT_MS = 20000;

/** ADR 0011 §9's credential-sync path (`sync_node_providers` with
 * `include_secrets: true` + `provider_ids`) — `authorize_and_sync`'s own
 * per-provider outcomes ARE `NodeProviderCredentialStatus`, already
 * pushed live via `node_provider_credential_upsert`
 * (`backend/adapters/system_adapter.py`'s `_on_credential_changed`); this
 * collects those frames for exactly the requested `node_id`/`provider_ids`
 * until every one reaches a terminal status (or the window elapses) and
 * assembles the SAME `MachineSyncResult{ok, results}` shape
 * `postMachineSync`'s REST parsing produces — event-driven completion,
 * not a synthesized synchronous response. `null` when the accept/reject
 * ack itself never resolves natively (mirrors every other native-when-
 * open helper's not-open contract). */
function nativeSyncProvidersToNode(
  nodeId: string, providerIds: string[],
): Promise<MachineSyncResult> | null {
  // Submit FIRST — `submitSystemIntent`'s own not-open check is
  // synchronous and side-effect-free; only once it confirms the
  // connection is actually usable does this start the (up to 20s) frame
  // collection below, so a not-open call never leaves a dangling
  // subscription/timer behind for the REST-fallback path it's about to
  // take instead.
  const native = submitSystemIntent({
    kind: "sync_node_providers",
    node_id: nodeId,
    include_secrets: true,
    provider_ids: providerIds,
  });
  if (!native) return null;
  const remaining = new Set(providerIds);
  const collected = new Map<string, NodeProviderCredentialStatusWire>();
  const collectionDone = new Promise<void>((resolve) => {
    let settled = false;
    const unsubscribe = subscribeSystemFrames((frame) => {
      if (settled || frame.type !== "node_provider_credential_upsert") return;
      const status = frame.status;
      if (status.node_id !== nodeId || !remaining.has(status.provider_id)) return;
      if (status.status === "pending") return;
      collected.set(status.provider_id, status);
      remaining.delete(status.provider_id);
      if (remaining.size === 0) {
        settled = true;
        clearTimeout(timer);
        unsubscribe();
        resolve();
      }
    });
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      unsubscribe();
      resolve();
    }, CREDENTIAL_SYNC_RESULT_TIMEOUT_MS);
  });
  return native.then(async (ack) => {
    if (ack.type === "intent_rejected") {
      return { ok: false, error: ack.message || ack.code };
    }
    await collectionDone;
    const results = providerIds.map((providerId) => {
      const status = collected.get(providerId);
      return {
        node_id: nodeId,
        provider_id: providerId,
        ok: status?.status === "synced",
        error: status?.status === "failed" ? status.failure_code ?? "sync_failed" : undefined,
      };
    });
    return { ok: results.every((result) => result.ok), results };
  });
}

/** Copy the primary provider list/default provider to a connected worker
 * node. Native-when-open ONLY for the credential-sync shape
 * (`options.includeSecrets` + a non-empty `options.providerIds` — see
 * `nativeSyncProvidersToNode`); `sync_node_providers` has no v2
 * representation for the plain, non-secret provider-list push (a one-shot
 * RPC ack with no durable state to re-derive from — see
 * `backend/surface_contract/intents.py`'s `SyncNodeProviders` docstring),
 * so that shape stays 100% legacy REST, same as before. */
export async function syncProvidersToNode(
  nodeId: string,
  options: ProviderSyncOptions = {},
): Promise<MachineSyncResult> {
  const providerIds = Array.isArray(options.providerIds)
    ? options.providerIds.map((id) => id.trim()).filter(Boolean)
    : [];
  if (options.includeSecrets && providerIds.length) {
    const native = nativeSyncProvidersToNode(nodeId, providerIds);
    if (native) return native;
  }
  const body = options.includeSecrets
    ? { include_secrets: true, provider_ids: providerIds }
    : undefined;
  return postMachineSync(
    `${machineNodesApi()}/nodes/${encodeURIComponent(nodeId)}/sync-providers`,
    body,
  );
}

/** Copy the primary provider list/default provider to every connected
 * worker node. No ADR 0011 intent covers a fan-out-to-all-nodes sync
 * (§9's `sync_node_providers` is single-`node_id` only) — stays 100%
 * legacy, a genuine absence. */
export async function syncProvidersToConnectedNodes(): Promise<MachineSyncResult> {
  return postMachineSync(`${machineNodesApi()}/nodes/sync-providers`);
}

/** Copy primary extension config/artifacts to a connected worker node. No
 * ADR 0011 intent covers extension sync to nodes at all (§9 is provider-
 * credential sync only) — stays 100% legacy, a genuine absence. */
export async function syncExtensionsToNode(nodeId: string): Promise<MachineSyncResult> {
  return postMachineSync(
    `${machineNodesApi()}/nodes/${encodeURIComponent(nodeId)}/sync-extensions`,
  );
}

/** Copy primary extension config/artifacts to every connected worker node.
 * Same absence as `syncExtensionsToNode` above. */
export async function syncExtensionsToConnectedNodes(): Promise<MachineSyncResult> {
  return postMachineSync(`${machineNodesApi()}/nodes/sync-extensions`);
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
