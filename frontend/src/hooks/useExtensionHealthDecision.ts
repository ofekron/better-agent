import { useCallback, useEffect, useRef, useState } from "react";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";

// Backend contract (GET /api/extensions): an extension record may carry a
// `pending_health_decision` when the runtime wants the user to decide
// whether a misbehaving extension (and its cohort) should stay enabled.
// POST /api/extensions/{extension_id}/health-decision records the decision;
// the catalog snapshot is invalidated server-side on ack, so a refetch that
// returns no pending decision is the authoritative "dismissed" signal.

export interface HealthDecisionCohortMember {
  extension_id: string;
  name?: string;
}

export interface PendingHealthDecision {
  id: string;
  reason: string;
  at: number;
  cohort: HealthDecisionCohortMember[];
  elapsed_seconds: number;
}

export type HealthDecisionAction = "disable" | "keep_enabled";

export interface ActiveHealthDecision {
  extensionId: string;
  extensionName: string;
  pending: PendingHealthDecision;
}

interface ExtensionHealthRecord {
  manifest?: { id?: string; name?: string };
  pending_health_decision?: PendingHealthDecision;
}

async function fetchPendingDecision(): Promise<ActiveHealthDecision | null> {
  try {
    const res = await fetch(`${API}/api/extensions?include_hidden=true`, { credentials: "include" });
    if (!res.ok) return null;
    const data = await res.json();
    const records: ExtensionHealthRecord[] = Array.isArray(data?.extensions) ? data.extensions : [];
    for (const record of records) {
      const pending = record.pending_health_decision;
      const id = record.manifest?.id;
      if (pending && typeof pending.id === "string" && id) {
        return { extensionId: id, extensionName: record.manifest?.name ?? id, pending };
      }
    }
  } catch {
    return null;
  }
  return null;
}

/** Reads GET /api/extensions, surfaces the first pending health decision,
 * and posts the user's choice. The prompt should be unmounted the moment a
 * refetch after an acknowledgement returns no pending decision. */
export function useExtensionHealthDecision(): {
  decision: ActiveHealthDecision | null;
  submit: (action: HealthDecisionAction) => Promise<boolean>;
} {
  const [decision, setDecision] = useState<ActiveHealthDecision | null>(null);
  const inFlightAck = useRef(false);

  const refresh = useCallback(async () => {
    setDecision(await fetchPendingDecision());
  }, []);

  useEffect(() => {
    // Initial mount fetch. setState happens after the network await (not
    // synchronously in the effect body), so this is the standard data-fetch
    // lifecycle, not a derived-state cascade.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
    const onfocus = () => void refresh();
    window.addEventListener("focus", onfocus);
    // Push-driven invalidation: the backend broadcasts `extension.catalog`
    // whenever the extension snapshot changes (incl. a health decision being
    // raised/cleared), so refetch immediately on it. Catalog frames are also
    // broadcast globally and are NOT replayed on WS reconnect, so rehydrate on
    // `ws_connection_changed` (connected) to close that gap — no polling.
    const offCatalog = eventBus.subscribe("extension.catalog", () => void refresh());
    const offConn = eventBus.subscribe("ws_connection_changed", (p) => {
      if (p && (p as { connected?: boolean }).connected) void refresh();
    });
    return () => {
      window.removeEventListener("focus", onfocus);
      offCatalog();
      offConn();
    };
  }, [refresh]);

  const submit = useCallback(
    async (action: HealthDecisionAction): Promise<boolean> => {
      if (!decision || inFlightAck.current) return false;
      inFlightAck.current = true;
      try {
        const res = await fetch(
          `${API}/api/extensions/${encodeURIComponent(decision.extensionId)}/health-decision`,
          {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ decision_id: decision.pending.id, action }),
          },
        );
        if (!res.ok) return false;
        // Disappear only after the backend ack + a refetch that confirms the
        // decision is cleared. If the backend still reports it pending, keep
        // showing so the user is never left in a false-success state.
        const next = await fetchPendingDecision();
        setDecision(next && next.pending.id === decision.pending.id ? decision : next);
        return true;
      } catch {
        return false;
      } finally {
        inFlightAck.current = false;
      }
    },
    [decision],
  );

  return { decision, submit };
}
