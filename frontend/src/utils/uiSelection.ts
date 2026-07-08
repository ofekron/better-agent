import type { Session } from "../types";
import { queueWrite } from "./writeBacklog";

// Per-machine UI navigation-restore state: the last project the user
// selected, and the last session viewed in each project×node. Better Agent
// runs locally, so the backend instance IS the machine — `ui_selection.json`
// is the single source of truth. This module mirrors it to localStorage
// purely as an OFFLINE first-paint cache and exposes synchronous reads for
// routing. Writes round-trip to the backend (PATCH); the backend broadcasts
// `ui_selection_changed` so other tabs converge their restore-cache. Tabs use
// the cache for cold-load restore only — never to force-navigate an active
// view.

const PROJECT_PATH_KEY = "better-agent-selected-project";
const PROJECT_NODE_KEY = "better-agent-selected-project-node";
const REMEMBERED_KEY = "better-agent-remembered-session-by-project";
const OPEN_SESSION_IDS_KEY = "better-agent-open-session-ids";

// Nested by project path, then node id. JSON object keys are opaque strings,
// so paths/node ids are stored verbatim with no escaping.
type ProjectMap = Record<string, Record<string, string>>;
export type SelectedProject = { path: string; node_id: string } | null;
export type UiSelectionSnapshot = {
  selected_project: SelectedProject;
  remembered_session_by_project: ProjectMap;
  open_session_tab_ids?: string[];
};

function normalizeSessionIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const id of value) {
    if (typeof id !== "string" || !id || seen.has(id)) continue;
    seen.add(id);
    out.push(id);
  }
  return out;
}

function readRememberedLS(): ProjectMap {
  try {
    const raw = localStorage.getItem(REMEMBERED_KEY);
    return raw ? (JSON.parse(raw) as ProjectMap) : {};
  } catch {
    return {};
  }
}

function readSelectedLS(): SelectedProject {
  const path = localStorage.getItem(PROJECT_PATH_KEY) || "";
  if (!path) return null;
  return { path, node_id: localStorage.getItem(PROJECT_NODE_KEY) || "primary" };
}

function readOpenSessionIdsLS(): string[] {
  try {
    return normalizeSessionIds(JSON.parse(localStorage.getItem(OPEN_SESSION_IDS_KEY) || "[]"));
  } catch {
    return [];
  }
}

// In-memory cache seeded synchronously from localStorage for instant first
// paint, then reconciled by the backend snapshot on mount + WS push.
let remembered: ProjectMap = readRememberedLS();
let selectedProject: SelectedProject = readSelectedLS();
let openSessionTabIds: string[] = readOpenSessionIdsLS();

function writeRememberedLS(): void {
  try {
    localStorage.setItem(REMEMBERED_KEY, JSON.stringify(remembered));
  } catch {
    // localStorage unavailable or full — cache is best-effort.
  }
}

function writeSelectedLS(): void {
  try {
    localStorage.setItem(PROJECT_PATH_KEY, selectedProject?.path ?? "");
    localStorage.setItem(PROJECT_NODE_KEY, selectedProject?.node_id ?? "primary");
  } catch {
    // best-effort
  }
}

function writeOpenSessionIdsLS(): void {
  try {
    localStorage.setItem(OPEN_SESSION_IDS_KEY, JSON.stringify(openSessionTabIds));
  } catch {
    // best-effort
  }
}

// Collapse key per top-level field so independent fields don't clobber each
// other when multiple writes queue offline (take_latest per key).
function patchCollapseKey(body: unknown): string {
  if (body && typeof body === "object") {
    if ("open_session_tab_ids" in body) return "ui_selection.open_session_tab_ids";
    if ("selected_project" in body) return "ui_selection.selected_project";
    const rs = (body as { remembered_session?: { path?: string; node_id?: string } })
      .remembered_session;
    if (rs) return `ui_selection.remembered_session:${rs.path ?? ""}:${rs.node_id ?? ""}`;
  }
  return "ui_selection.misc";
}

// Write-through to the backend via the durable backlog: on success the
// backend persists + broadcasts `ui_selection_changed`; on failure the write
// stays queued (localStorage-backed) and drains on reconnect. The module
// var + localStorage cache already hold the optimistic value for first paint
// and cold-load restore.
function patch(body: unknown): void {
  queueWrite({
    method: "PATCH",
    url: "/api/ui-selection",
    body,
    key: patchCollapseKey(body),
  });
}

export function getSelectedProject(): SelectedProject {
  return selectedProject;
}

export function setSelectedProject(path: string, nodeId: string): void {
  const next: SelectedProject = path
    ? { path, node_id: nodeId || "primary" }
    : null;
  if (
    (next?.path ?? "") === (selectedProject?.path ?? "") &&
    (next?.node_id ?? "") === (selectedProject?.node_id ?? "")
  ) {
    return;
  }
  selectedProject = next;
  writeSelectedLS();
  patch({ selected_project: next });
}

export function getRememberedSessionId(
  path: string,
  nodeId: string,
): string | null {
  return remembered[path]?.[nodeId] ?? null;
}

export function setRememberedSessionId(
  path: string,
  nodeId: string,
  sessionId: string,
): void {
  if (remembered[path]?.[nodeId] === sessionId) return;
  remembered[path] = { ...(remembered[path] ?? {}), [nodeId]: sessionId };
  writeRememberedLS();
  patch({ remembered_session: { path, node_id: nodeId, session_id: sessionId } });
}

export function getOpenSessionTabIds(): string[] {
  const cached = readOpenSessionIdsLS();
  if (
    cached.length !== openSessionTabIds.length ||
    cached.some((id, index) => id !== openSessionTabIds[index])
  ) {
    openSessionTabIds = cached;
  }
  return [...openSessionTabIds];
}

export function setOpenSessionTabIds(sessionIds: string[]): void {
  const next = normalizeSessionIds(sessionIds);
  if (
    next.length === openSessionTabIds.length &&
    next.every((id, index) => id === openSessionTabIds[index])
  ) {
    return;
  }
  openSessionTabIds = next;
  writeOpenSessionIdsLS();
  patch({ open_session_tab_ids: next });
}

export function cacheOpenSessionTabIds(sessionIds: string[]): void {
  const next = normalizeSessionIds(sessionIds);
  if (
    next.length === openSessionTabIds.length &&
    next.every((id, index) => id === openSessionTabIds[index])
  ) {
    return;
  }
  openSessionTabIds = next;
  writeOpenSessionIdsLS();
}

// Reconcile the cache from a backend snapshot (mount GET or WS push). Backend
// is authoritative; the union keeps any local-only entry the backend has not
// received yet (in-flight write). Does NOT mutate React state or navigate —
// callers decide how to use the refreshed cache. When `seedUp` is set (mount
// only), local-only entries are pushed to the backend so a user upgrading
// from the localStorage-only version migrates their state once.
export function applyBackendSnapshot(
  snap: UiSelectionSnapshot,
  seedUp = false,
): void {
  const backendMap =
    snap.remembered_session_by_project &&
    typeof snap.remembered_session_by_project === "object"
      ? snap.remembered_session_by_project
      : {};
  const backendOpenIds = normalizeSessionIds(snap.open_session_tab_ids);

  const merged: ProjectMap = {};
  const paths = new Set([
    ...Object.keys(remembered),
    ...Object.keys(backendMap),
  ]);
  for (const p of paths) {
    merged[p] = { ...(remembered[p] ?? {}), ...(backendMap[p] ?? {}) };
  }

  if (seedUp) {
    for (const [p, byNode] of Object.entries(remembered)) {
      for (const [nodeId, sid] of Object.entries(byNode)) {
        if (backendMap[p]?.[nodeId] === undefined) {
          patch({ remembered_session: { path: p, node_id: nodeId, session_id: sid } });
        }
      }
    }
  }

  remembered = merged;
  writeRememberedLS();

  if (seedUp && openSessionTabIds.length > 0) {
    const mergedOpenIds = normalizeSessionIds([...backendOpenIds, ...openSessionTabIds]);
    openSessionTabIds = mergedOpenIds;
    if (
      mergedOpenIds.length !== backendOpenIds.length ||
      mergedOpenIds.some((id, index) => id !== backendOpenIds[index])
    ) {
      patch({ open_session_tab_ids: mergedOpenIds });
    }
  } else {
    openSessionTabIds = backendOpenIds;
  }
  writeOpenSessionIdsLS();

  if (snap.selected_project) {
    selectedProject = snap.selected_project;
    writeSelectedLS();
  } else if (seedUp && selectedProject) {
    patch({ selected_project: selectedProject });
  }
}

function belongsToProject(s: Session, path: string, nodeId: string): boolean {
  return (
    s.cwd === path && (s.node_id || "primary") === nodeId && !s.archived
  );
}

// Pick the session to show when entering a project: the remembered one if
// still present and valid, otherwise the first session in that project.
export function pickSessionForProject(
  sessions: Session[],
  path: string,
  nodeId: string,
  rememberedId: string | null,
): Session | null {
  if (rememberedId) {
    const found = sessions.find(
      (s) => s.id === rememberedId && belongsToProject(s, path, nodeId),
    );
    if (found) return found;
  }
  return sessions.find((s) => belongsToProject(s, path, nodeId)) ?? null;
}
