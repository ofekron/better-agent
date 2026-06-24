import type { Session } from "../types";

// Last session the user viewed in each project, so switching back to a
// project reopens that session instead of always the first one. Pure UI
// preference (like sidebar width) — not backend-owned state.
const STORAGE_KEY = "better-agent-remembered-session-by-project";

// Nested by project path, then node id. JSON object keys are opaque
// strings, so paths/node ids are stored verbatim with no escaping.
type ProjectMap = Record<string, Record<string, string>>;

function readMap(): ProjectMap {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as ProjectMap) : {};
  } catch {
    return {};
  }
}

function writeMap(map: ProjectMap): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // localStorage unavailable or full — preference is best-effort.
  }
}

export function getRememberedSessionId(
  path: string,
  nodeId: string,
): string | null {
  return readMap()[path]?.[nodeId] ?? null;
}

export function setRememberedSessionId(
  path: string,
  nodeId: string,
  sessionId: string,
): void {
  const map = readMap();
  if (map[path]?.[nodeId] === sessionId) return;
  map[path] = { ...(map[path] ?? {}), [nodeId]: sessionId };
  writeMap(map);
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
    const remembered = sessions.find(
      (s) => s.id === rememberedId && belongsToProject(s, path, nodeId),
    );
    if (remembered) return remembered;
  }
  return sessions.find((s) => belongsToProject(s, path, nodeId)) ?? null;
}
