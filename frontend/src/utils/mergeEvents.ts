import type { WSEvent, WorkerPanel, TaggedEvent, EntityBlock } from "../types";

/** Extract a best-effort timestamp from an event. */
function eventTimestamp(e: WSEvent): string | undefined {
  if (typeof e._ts === "string") return e._ts;
  const msg = e.data?.message as Record<string, unknown> | undefined;
  if (msg && typeof msg.timestamp === "string") return msg.timestamp;
  if (typeof e.data?.timestamp === "string") return e.data.timestamp as string;
  return undefined;
}

export function panelKindLabel(kind: WorkerPanel["panel_kind"] | undefined): string {
  if (kind === "sub_session_created") return "Sub Session Created";
  if (kind === "session_created") return "Session Created";
  if (kind === "sub_session") return "Sub Session";
  if (kind === "session") return "Session";
  return "Worker";
}

export function isCreationPanelKind(kind: WorkerPanel["panel_kind"] | undefined): boolean {
  return kind === "sub_session_created" || kind === "session_created";
}

/**
 * Build stable timeline streams. Each worker panel is a single contiguous
 * collapsible block, inserted at the point in the manager stream where its
 * delegation occurred — anchored by `insert_at` (the manager-event count at
 * delegation time), not by wall-clock timestamp. Timestamps proved
 * unreliable: `started_at` is absent on many panels and manager-event
 * timestamps use inconsistent formats, so a timestamp merge parked panels
 * at the bottom. `insert_at` is stamped once by the backend (single source
 * of truth), identical across live, reload, and restore.
 */
export function tagEvents(
  managerEvents: WSEvent[],
  workers: WorkerPanel[],
): TaggedEvent[] {
  const result: TaggedEvent[] = [];
  let seq = 0;
  let managerIndex = 0;

  // Order panels by their delegation point. Legacy panels without
  // `insert_at` sort to the end in creation order — no worse than before.
  const ordered = workers
    .map((worker, index) => ({
      worker,
      index,
      insertAt:
        typeof worker.insert_at === "number"
          ? worker.insert_at
          : Number.POSITIVE_INFINITY,
    }))
    .sort((a, b) => {
      if (a.insertAt !== b.insertAt) return a.insertAt - b.insertAt;
      return a.index - b.index;
    });

  const pushManager = (event: WSEvent) => {
    result.push({
      entityType: "manager",
      entityId: "manager",
      entityLabel: "Manager",
      event,
      seq: seq++,
    });
  };

  const pushWorker = (w: WorkerPanel) => {
    const entityId = w.delegation_id;
    const entityLabel = w.worker_description || panelKindLabel(w.panel_kind);
    if (w.events.length === 0) {
      result.push({
        event: {
          type: "worker_start",
          data: { timestamp: w.started_at ?? "" },
        },
        entityType: "worker",
        entityId,
        entityLabel,
        panelKind: w.panel_kind,
        startedAt: w.started_at,
        seq: seq++,
      });
      return;
    }
    for (const event of w.events) {
      result.push({
        event,
        entityType: "worker",
        entityId,
        entityLabel,
        panelKind: w.panel_kind,
        startedAt: w.started_at,
        seq: seq++,
      });
    }
  };

  for (const { worker, insertAt } of ordered) {
    // Flush the manager events that preceded this delegation, then emit
    // the panel inline. `insertAt` may exceed the available events
    // (counted on a stale snapshot) — clamp so the panel still lands at
    // the end of the known stream rather than past it.
    const stop = Math.min(insertAt, managerEvents.length);
    while (managerIndex < stop) {
      pushManager(managerEvents[managerIndex]);
      managerIndex += 1;
    }
    pushWorker(worker);
  }

  while (managerIndex < managerEvents.length) {
    pushManager(managerEvents[managerIndex]);
    managerIndex += 1;
  }

  return result;
}

export function dedupeWorkerPanels(workers: WorkerPanel[]): WorkerPanel[] {
  const seen = new Set<string>();
  const deduped: WorkerPanel[] = [];
  for (const worker of workers) {
    const key = worker.delegation_id;
    if (!key || seen.has(key)) continue;
    seen.add(key);
    deduped.push({
      ...worker,
      events: Array.isArray(worker.events) ? worker.events : [],
    });
  }
  return deduped;
}

/** Group consecutive TaggedEvents with the same entity into EntityBlocks. */
export function groupByEntity(tagged: TaggedEvent[]): EntityBlock[] {
  if (tagged.length === 0) return [];

  const blocks: EntityBlock[] = [];
  let current: EntityBlock = {
    entityType: tagged[0].entityType,
    entityId: tagged[0].entityId,
    entityLabel: tagged[0].entityLabel,
    panelKind: tagged[0].panelKind,
    startedAt: tagged[0].startedAt,
    events: [],
    timestamps: [],
  };

  for (const t of tagged) {
    if (t.entityId === current.entityId) {
      current.events.push(t.event);
      current.timestamps.push(eventTimestamp(t.event));
    } else {
      blocks.push(current);
      current = {
        entityType: t.entityType,
        entityId: t.entityId,
        entityLabel: t.entityLabel,
        panelKind: t.panelKind,
        startedAt: t.startedAt,
        events: [t.event],
        timestamps: [eventTimestamp(t.event)],
      };
    }
  }
  blocks.push(current);
  return blocks;
}
