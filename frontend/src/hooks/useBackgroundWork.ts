import { useCallback, useEffect, useRef, useState } from "react";
import { API } from "../api";
import { eventBus, type BusEventMap } from "src/lib/eventBus";
import type { BackgroundWorkItem, BackgroundWorkSnapshot } from "../types";

/** Projection of the backend's background work registry.
 *
 * `GET /api/background-work` is authoritative; `background_work_changed`
 * carries deltas. Three rules keep the projection truthful:
 *
 * 1. Deltas are applied only when strictly newer than what is held for that
 *    id (`seq`), so a delta that raced the snapshot response cannot roll a
 *    row backwards.
 * 2. A delta whose `epoch` differs from the snapshot's means the registry
 *    was rebuilt (backend restart or reload). The whole map is discarded and
 *    re-fetched rather than merged — `seq` restarts at 0 on a new epoch, so
 *    merging would let stale higher-seq rows win forever.
 * 3. Reconnecting re-fetches, because deltas emitted while the socket was
 *    down were never queued anywhere.
 */

export interface BackgroundWorkState {
  items: BackgroundWorkItem[];
  /** False while the socket is down: rows are last-known, not live. */
  live: boolean;
  dismiss: (id: string) => void;
}

export function useBackgroundWork(): BackgroundWorkState {
  const [items, setItems] = useState<Record<string, BackgroundWorkItem>>({});
  const [live, setLive] = useState(true);
  const epochRef = useRef<string | null>(null);
  // Guards against an in-flight snapshot landing after a newer one and
  // resurrecting the state it replaced.
  const fetchSeq = useRef(0);

  const refetch = useCallback(() => {
    const ticket = ++fetchSeq.current;
    fetch(`${API}/api/background-work`)
      .then((r) => r.json())
      .then((snapshot: unknown) => {
        if (ticket !== fetchSeq.current) return;
        // fetch() does not reject on 4xx/5xx, so an auth-gated 401 body or
        // any schema drift arrives here as the wrong shape. Reject it rather
        // than letting a non-iterable reach the render.
        if (
          !snapshot
          || typeof snapshot !== "object"
          || !Array.isArray((snapshot as BackgroundWorkSnapshot).items)
        ) return;
        const typed = snapshot as BackgroundWorkSnapshot;
        epochRef.current = typed.epoch;
        const next: Record<string, BackgroundWorkItem> = {};
        for (const item of typed.items) {
          if (item?.id) next[item.id] = item;
        }
        setItems(next);
      })
      .catch(() => {
        // Backend not accepting REST yet (still booting). Deltas repopulate
        // as they arrive, and the next reconnect re-fetches.
      });
  }, []);

  useEffect(() => { refetch(); }, [refetch]);

  useEffect(() => {
    function onDelta(detail: BusEventMap["background_work_changed"]) {
      if (!detail || typeof detail !== "object") return;
      if (epochRef.current !== null && detail.epoch !== epochRef.current) {
        refetch();
        return;
      }
      if ("cleared" in detail && detail.cleared) {
        setItems({});
        return;
      }
      if ("removed_id" in detail) {
        const removedId = detail.removed_id;
        setItems((prev) => {
          if (!(removedId in prev)) return prev;
          const next = { ...prev };
          delete next[removedId];
          return next;
        });
        return;
      }
      const item = "item" in detail ? detail.item : undefined;
      if (!item?.id) return;
      setItems((prev) => {
        const held = prev[item.id];
        if (held && held.seq >= item.seq) return prev;
        return { ...prev, [item.id]: item };
      });
    }
    return eventBus.subscribe("background_work_changed", onDelta);
  }, [refetch]);

  useEffect(() => {
    function onConnection(detail: BusEventMap["ws_connection_changed"]) {
      const connected = !!detail?.connected;
      setLive(connected);
      if (connected) refetch();
    }
    return eventBus.subscribe("ws_connection_changed", onConnection);
  }, [refetch]);

  // Terminal rows are evicted after their own server-stamped retention
  // window. Driving this off `finished_at` rather than a timer per row keeps
  // the schedule correct when a delta lands between effect runs. Failures
  // are never auto-evicted — they wait for the user.
  useEffect(() => {
    const interval = window.setInterval(() => {
      setItems((prev) => {
        const now = Date.now();
        let changed = false;
        const next: Record<string, BackgroundWorkItem> = {};
        for (const [id, item] of Object.entries(prev)) {
          if (item.status === "succeeded" && item.finished_at) {
            const finishedMs = Date.parse(item.finished_at);
            if (!Number.isNaN(finishedMs) && now - finishedMs > item.retention_ms) {
              changed = true;
              continue;
            }
          }
          next[id] = item;
        }
        return changed ? next : prev;
      });
    }, 500);
    return () => window.clearInterval(interval);
  }, []);

  // Local-only removal. The row is gone from this client's view; the backend
  // remains the owner and will re-announce if the work changes again.
  const dismiss = useCallback((id: string) => {
    setItems((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  const ordered = Object.values(items).sort(
    (a, b) => Date.parse(a.started_at) - Date.parse(b.started_at),
  );
  return { items: ordered, live, dismiss };
}
