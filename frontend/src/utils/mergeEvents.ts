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

/** MCP tool short names (suffix after the last `__`) that spawn a panel
 * 1:1 in the SAME assistant message they fire in. `create_worker` is
 * excluded: it's approval-gated and its worker panel appears later via a
 * separate delegation (ask/delegate), so its tool_use has no same-message
 * panel and would desync the positional match. */
const DELEGATION_TOOL_SHORT_NAMES = new Set([
  "ask",
  "mssg",
  "delegate_task",
  "create_session",
  "create_sub_session",
]);

function toolShortName(name: string): string {
  const idx = name.lastIndexOf("__");
  return idx === -1 ? name : name.slice(idx + 2);
}

/** Delegation tool_use blocks across the manager stream, in firing order.
 * Each carries the index of the event ENTRY that contains it. Multiple
 * delegation tool_use blocks in one entry (parallel asks) yield multiple
 * records sharing that entry index. */
function delegationToolUses(
  managerEvents: WSEvent[],
): { entryIndex: number; short: string }[] {
  const out: { entryIndex: number; short: string }[] = [];
  managerEvents.forEach((ev, entryIndex) => {
    if (ev.type !== "agent_message") return;
    const data = ev.data as
      | { type?: string; message?: { content?: unknown } }
      | undefined;
    if (!data || data.type !== "assistant") return;
    const content = data.message?.content;
    if (!Array.isArray(content)) return;
    for (const raw of content) {
      if (!raw || typeof raw !== "object") continue;
      const block = raw as { type?: string; name?: string };
      if (block.type !== "tool_use" || typeof block.name !== "string") continue;
      const short = toolShortName(block.name);
      if (DELEGATION_TOOL_SHORT_NAMES.has(short)) out.push({ entryIndex, short });
    }
  });
  return out;
}

function panelMatchesTool(short: string, w: WorkerPanel): boolean {
  switch (short) {
    case "create_sub_session":
      return w.panel_kind === "sub_session_created";
    case "create_session":
      return w.panel_kind === "session_created";
    case "ask":
      return w.run_mode === "team_ask" || w.run_mode === "fork";
    case "mssg":
    case "delegate_task":
      return w.run_mode === "team_message";
    default:
      return false;
  }
}

/**
 * Render-stable anchor per panel: the index right after the event entry
 * holding the tool_use that triggered the delegation. Derived here instead
 * of trusting the backend-stamped `insert_at`, which is captured
 * synchronously at MCP-tool-fire time — BEFORE the triggering tool_use
 * event has been tail-appended to the message — so it lands ahead of its
 * own tool call (e.g. a `create_sub_session → ask` sub-session group
 * rendering before `create_sub_session`). Panels iterate in firing (append)
 * order and consume compatible delegation tool_use blocks positionally; a
 * panel with no compatible tool_use in this message (e.g. a Codex native
 * subagent) is left out and falls back to its stored `insert_at`.
 */
export function derivePanelAnchors(
  managerEvents: WSEvent[],
  workers: WorkerPanel[],
): Map<string, number> {
  const toolUses = delegationToolUses(managerEvents);
  const anchors = new Map<string, number>();
  let cursor = 0;
  for (const w of workers) {
    if (cursor < toolUses.length && panelMatchesTool(toolUses[cursor].short, w)) {
      anchors.set(w.delegation_id, toolUses[cursor].entryIndex + 1);
      cursor += 1;
    }
  }
  return anchors;
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

  // Order panels by their delegation point. The anchor is derived from the
  // triggering tool_use position (render-stable); the backend-stamped
  // `insert_at` is the fallback for panels with no matching tool_use, and
  // legacy panels without either sort to the end in creation order.
  const anchors = derivePanelAnchors(managerEvents, workers);
  const ordered = workers
    .map((worker, index) => {
      const derived = anchors.get(worker.delegation_id);
      const insertAt =
        typeof derived === "number"
          ? derived
          : typeof worker.insert_at === "number"
            ? worker.insert_at
            : Number.POSITIVE_INFINITY;
      return { worker, index, insertAt };
    })
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
