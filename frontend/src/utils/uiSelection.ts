import type { Session } from "../types";
import { API } from "../api";

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

// Nested by project path, then node id. JSON object keys are opaque strings,
// so paths/node ids are stored verbatim with no escaping.
type ProjectMap = Record<string, Record<string, string>>;
export type SelectedProject = { path: string; node_id: string } | null;
export type UiSelectionSnapshot = {
  selected_project: SelectedProject;
  remembered_session_by_project: ProjectMap;
};

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

// In-memory cache seeded synchronously from localStorage for instant first
// paint, then reconciled by the backend snapshot on mount + WS push.
let remembered: ProjectMap = readRememberedLS();
let selectedProject: SelectedProject = readSelectedLS();

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

// Fire-and-forget; backend persists + broadcasts ui_selection_changed.
function patch(body: unknown): void {
  fetch(`${API}/api/ui-selection`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => {
    // Offline — the localStorage cache already holds the change so the next
    // cold load still restores. Backend reconciles on the next write.
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
